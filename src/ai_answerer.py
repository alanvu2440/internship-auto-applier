"""
AI Answerer Module

Uses Google Gemini to answer custom/unknown job application questions.
Has config-based fallback for common questions when AI is unavailable.
"""

import os
import re
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List
from loguru import logger

from question_verifier import QuestionVerifier

# Optional Google AI import
try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    genai = None
    GENAI_AVAILABLE = False
    logger.warning("google-generativeai not installed - AI features disabled, using config fallback only")


class AIAnswerer:
    """Uses Gemini AI to answer custom application questions with config fallback."""

    SYSTEM_PROMPT = """You are an expert at filling out job applications. Your task is to answer application questions on behalf of the candidate using their profile information.

Guidelines:
- Be concise but thorough (2-4 sentences typically)
- Be professional and enthusiastic
- Highlight relevant skills and experience
- Be honest - don't make up information
- If the question requires specific info not in the profile, give a reasonable generic answer
- For yes/no questions, answer clearly then briefly explain if needed
- Match the tone expected for the role (more casual for startups, formal for enterprise)

Candidate Profile:
{profile}

Job Details:
Company: {company}
Role: {role}
"""

    def __init__(self, config_or_api_key=None, model: str = "gemini-2.5-flash", api_key: Optional[str] = None,
                 secrets: Optional[Dict[str, Any]] = None):
        """Initialize AI answerer.

        Args:
            config_or_api_key: Either a config dict or an API key string
            model: Gemini model name
            api_key: Explicit API key (takes precedence)
            secrets: Secrets dict with backup key and budget cap
        """
        # Handle both AIAnswerer(config_dict) and AIAnswerer(api_key="...")
        if isinstance(config_or_api_key, dict):
            self.config = config_or_api_key
            self.api_key = api_key or os.getenv("GEMINI_API_KEY")
            # Auto-set profile from config
            self.profile_str = ""
            self.set_profile(config_or_api_key)
        else:
            self.config = {}
            self.api_key = config_or_api_key or api_key or os.getenv("GEMINI_API_KEY")
            self.profile_str = ""

        self.model_name = model
        self._model = None
        self.job_context = {"company": "", "role": ""}
        self._retry_count = 2  # Reduced from 3 to avoid long waits
        self._retry_delay = 2
        self._ai_available = True
        self._ai_timeout = 15  # 15 second timeout for AI calls

        # Per-template question banks (loaded lazily)
        self._template_banks = {}  # ats_type → {normalized_question → {answer, type}}
        self._common_bank = None  # Cross-ATS common questions
        self._banks_loaded = False

        # Cached OptionMatcher (avoid re-instantiation on every call)
        self._option_matcher = None

        # Backup Gemini key (GCP $300 credit account)
        self._secrets = secrets or {}
        self._backup_api_key = self._secrets.get("gemini_backup_api_key", "") or ""
        self._budget_cap = float(self._secrets.get("gemini_backup_budget_cap", 300.0))
        self._using_backup = False
        self._primary_exhausted = False

        # Cost tracking — persisted to disk
        self._cost_path = Path("data/gemini_cost_tracker.json")
        self._cost_data = self._load_cost_tracker()

        # Answer cache — saves AI answers to disk so identical questions are instant
        self._cache_path = Path("data/answer_cache.json")
        self._answer_cache: Dict[str, str] = {}
        self._load_answer_cache()

        # Question knowledge base — logs every question + answer to markdown
        self._kb_path = Path("data/question_knowledge_base.md")
        self._kb_questions: set = set()
        self._load_kb()

        # Track all questions answered this session (for reporting)
        self.session_answers: List[Dict[str, str]] = []

        # Question verification system (human-in-the-loop)
        self.verifier = QuestionVerifier()

        if self.api_key and GENAI_AVAILABLE and isinstance(self.api_key, str):
            genai.configure(api_key=self.api_key)

    def _load_cost_tracker(self) -> Dict[str, Any]:
        """Load cost tracking data from disk."""
        try:
            if self._cost_path.exists():
                with open(self._cost_path) as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Could not load cost tracker: {e}")
        return {
            "total_calls_primary": 0,
            "total_calls_backup": 0,
            "total_input_tokens_backup": 0,
            "total_output_tokens_backup": 0,
            "estimated_cost_backup_usd": 0.0,
            "budget_cap_usd": self._budget_cap,
            "sessions": []
        }

    def _save_cost_tracker(self):
        """Save cost tracking data to disk."""
        try:
            self._cost_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cost_path, "w") as f:
                json.dump(self._cost_data, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save cost tracker: {e}")

    def _track_ai_call(self, is_backup: bool, input_len: int = 0, output_len: int = 0):
        """Track an AI call for cost monitoring."""
        if is_backup:
            self._cost_data["total_calls_backup"] += 1
            # Gemini 2.0 Flash pricing: ~$0.10/1M input, ~$0.40/1M output tokens
            # Rough estimate: 4 chars per token
            input_tokens = input_len // 4
            output_tokens = output_len // 4
            self._cost_data["total_input_tokens_backup"] += input_tokens
            self._cost_data["total_output_tokens_backup"] += output_tokens
            cost = (input_tokens * 0.10 / 1_000_000) + (output_tokens * 0.40 / 1_000_000)
            self._cost_data["estimated_cost_backup_usd"] += cost
            self._cost_data["estimated_cost_backup_usd"] = round(self._cost_data["estimated_cost_backup_usd"], 6)
        else:
            self._cost_data["total_calls_primary"] += 1
        self._save_cost_tracker()

    def _is_backup_budget_exceeded(self) -> bool:
        """Check if backup key budget cap has been reached."""
        return self._cost_data["estimated_cost_backup_usd"] >= self._budget_cap

    def _switch_to_backup_key(self) -> bool:
        """Switch to backup Gemini API key. Returns True if successful."""
        if not self._backup_api_key:
            logger.warning("No backup Gemini API key configured in secrets.yaml")
            return False
        if self._is_backup_budget_exceeded():
            logger.warning(f"Backup key budget cap reached (${self._cost_data['estimated_cost_backup_usd']:.2f} / ${self._budget_cap:.2f})")
            return False
        logger.info(f"Switching to backup Gemini API key (spent ${self._cost_data['estimated_cost_backup_usd']:.2f} / ${self._budget_cap:.2f} cap)")
        self.api_key = self._backup_api_key
        self._using_backup = True
        self._model = None  # Force re-init model with new key
        if GENAI_AVAILABLE:
            genai.configure(api_key=self.api_key)
        return True

    def _load_answer_cache(self):
        """Load cached answers from disk."""
        try:
            if self._cache_path.exists():
                with open(self._cache_path) as f:
                    self._answer_cache = json.load(f)
                logger.debug(f"Loaded {len(self._answer_cache)} cached answers")
        except Exception as e:
            logger.debug(f"Could not load answer cache: {e}")
            self._answer_cache = {}

    def _save_answer_cache(self):
        """Save answer cache to disk."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._answer_cache, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save answer cache: {e}")

    def _load_kb(self):
        """Load existing question knowledge base to avoid duplicates."""
        try:
            if self._kb_path.exists():
                content = self._kb_path.read_text()
                for line in content.split("\n"):
                    if line.startswith("**Q:**"):
                        self._kb_questions.add(line.replace("**Q:**", "").strip().lower())
        except Exception as e:
            logger.debug(f"Could not load KB: {e}")

    def _log_to_kb(self, question: str, answer: str, source: str, company: str = "", field_type: str = "text"):
        """Log a question + answer to the knowledge base markdown file."""
        q_key = question.strip().lower()[:100]
        if q_key in self._kb_questions:
            return
        self._kb_questions.add(q_key)
        try:
            self._kb_path.parent.mkdir(parents=True, exist_ok=True)
            header = ""
            if not self._kb_path.exists():
                header = "# Question Knowledge Base\n\nEvery question encountered during applications, with the answer used.\nEdit answers here, then copy to `config/master_config.yaml` under `common_answers`.\n\n---\n\n"
            with open(self._kb_path, "a") as f:
                f.write(header)
                f.write(f"**Q:** {question.strip()}\n")
                f.write(f"**A:** {answer.strip()}\n")
                f.write(f"**Source:** {source} | **Type:** {field_type} | **Company:** {company or self.job_context.get('company', 'unknown')}\n\n---\n\n")
        except Exception as e:
            logger.debug(f"Could not log to KB: {e}")

    def _cache_key(self, question: str, field_type: str, options: Optional[list] = None) -> str:
        """Generate a cache key for a question."""
        # Normalize: lowercase, strip whitespace, remove extra spaces
        q = re.sub(r'\s+', ' ', question.lower().strip())
        key = f"{q}|{field_type}"
        if options:
            key += f"|{'|'.join(sorted(o.lower().strip() for o in options))}"
        return key

    def _sub_company(self, text: str) -> str:
        """Replace [Company] placeholder with the current job's company name."""
        company = self.job_context.get("company", "") if self.job_context else ""
        if company and "[Company]" in text:
            return text.replace("[Company]", company)
        return text

    def _to_template(self, text: str) -> str:
        """Replace the current company name with [Company] before caching,
        so the answer can be reused across companies."""
        company = self.job_context.get("company", "") if self.job_context else ""
        if company and company != "Unknown" and len(company) > 2:
            return text.replace(company, "[Company]")
        return text

    def _load_template_banks(self):
        """Load per-ATS question banks from config/question_banks/."""
        if self._banks_loaded:
            return
        self._banks_loaded = True
        import yaml
        banks_dir = Path("config/question_banks")
        if not banks_dir.exists():
            return
        for yaml_file in banks_dir.glob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f) or {}
                ats_type = data.get("ats_type", yaml_file.stem)
                questions = data.get("questions", {})
                self._template_banks[ats_type] = questions
                if ats_type == "common" or yaml_file.stem == "common":
                    self._common_bank = questions
                logger.debug(f"Loaded {len(questions)} questions from {yaml_file.name}")
            except Exception as e:
                logger.debug(f"Failed to load {yaml_file}: {e}")

    def _lookup_template_bank(self, question: str, ats_type: str = None) -> Optional[dict]:
        """Look up a question in per-template banks.

        Returns {answer, type} if found, None otherwise.
        Priority: common bank (cross-ATS) → template-specific bank.
        Common bank is checked first so cross-bank learned answers are found immediately.
        """
        self._load_template_banks()
        norm = re.sub(r'\s+', ' ', question.lower().strip())
        norm = re.sub(r'[*\n\r]', '', norm).strip()

        # 1. Try common bank FIRST (cross-ATS answers available immediately)
        if self._common_bank:
            if norm in self._common_bank:
                return self._common_bank[norm]
            norm_clean = norm.rstrip('?. ')
            for key, val in self._common_bank.items():
                if key.startswith(norm_clean[:40]) or norm_clean.startswith(key[:40]):
                    return val

        # 2. Try template-specific bank
        if ats_type and ats_type in self._template_banks:
            bank = self._template_banks[ats_type]
            if norm in bank:
                return bank[norm]
            # Try fuzzy: strip trailing punctuation and try prefix match
            norm_clean = norm.rstrip('?. ')
            for key, val in bank.items():
                if key.startswith(norm_clean[:40]) or norm_clean.startswith(key[:40]):
                    return val

        return None

    def _normalize_for_bank(self, question: str) -> str:
        """Normalize question text for template bank storage/lookup."""
        norm = re.sub(r'\s+', ' ', question.lower().strip())
        norm = re.sub(r'[*\n\r]', '', norm).strip()
        return norm

    def _auto_learn_to_template_bank(self, question: str, answer: str, field_type: str, source: str):
        """Auto-add successfully answered questions to the template bank.

        Only adds high-confidence answers. The template bank grows continuously —
        same question in different phrasings all map to the correct answer.
        Like method overloading: multiple signatures, same behavior.
        """
        # Only learn from high-confidence sources
        approved_sources = {"config_option", "config", "config_fallback", "ai", "ai_backup", "verified"}
        if source not in approved_sources:
            return

        # Skip empty, placeholder, or too-short answers
        if not answer or len(answer.strip()) < 1:
            return
        if len(question.strip()) < 5:
            return

        # Normalize
        norm_q = self._normalize_for_bank(question)
        if not norm_q:
            return

        # Determine which bank to write to
        ats_type = self.job_context.get("ats_type", "")
        bank_file = None
        bank_dict = None

        if ats_type:
            bank_path = Path(f"config/question_banks/{ats_type}.yaml")
            if bank_path.exists():
                bank_file = bank_path
                bank_dict = self._template_banks.get(ats_type, {})
            else:
                bank_file = Path("config/question_banks/common.yaml")
                bank_dict = self._common_bank or {}
        else:
            bank_file = Path("config/question_banks/common.yaml")
            bank_dict = self._common_bank or {}

        # Check if already in bank (exact or prefix match)
        if norm_q in bank_dict:
            return
        norm_clean = norm_q.rstrip('?. ')
        for key in bank_dict:
            if key.startswith(norm_clean[:40]) or norm_clean.startswith(key[:40]):
                return  # Similar question already exists

        # Templatize answer (replace company name with placeholder for reuse)
        company = self.job_context.get("company", "")
        templatized = answer
        if company and len(company) > 2:
            templatized = answer.replace(company, "[Company]")

        # Add to in-memory bank
        entry = {"answer": templatized, "type": field_type or "text"}
        bank_dict[norm_q] = entry

        # Update in-memory reference for ATS-specific bank
        if ats_type and ats_type in self._template_banks:
            self._template_banks[ats_type][norm_q] = entry

        # ALWAYS update common bank in-memory (cross-bank learning)
        if self._common_bank is not None:
            self._common_bank[norm_q] = entry

        # Append to YAML file (append-only for performance)
        def _write_to_bank_file(filepath):
            safe_answer = templatized.replace("'", "''")
            with open(filepath, "a", encoding="utf-8") as f:
                if "'" in norm_q:
                    safe_question = norm_q.replace('"', '\\"')
                    f.write(f'\n  "{safe_question}":\n')
                else:
                    f.write(f"\n  {norm_q}:\n")
                f.write(f"    answer: '{safe_answer}'\n")
                f.write(f"    type: {field_type or 'text'}\n")

        # Write to primary bank file
        try:
            _write_to_bank_file(bank_file)
            logger.debug(f"Auto-learned question to {bank_file.name}: '{norm_q[:50]}' -> '{templatized[:30]}'")
        except Exception as e:
            logger.debug(f"Failed to auto-learn question to {bank_file}: {e}")

        # Cross-bank: also write to common.yaml if we wrote to an ATS-specific bank
        common_path = Path("config/question_banks/common.yaml")
        if bank_file != common_path and common_path.exists():
            try:
                _write_to_bank_file(common_path)
                logger.debug(f"Cross-bank learned to common.yaml: '{norm_q[:50]}'")
            except Exception as e:
                logger.debug(f"Failed to cross-bank learn to common.yaml: {e}")

    def _get_model(self):
        """Get or create Gemini model."""
        if not GENAI_AVAILABLE:
            return None
        if self._model is None:
            self._model = genai.GenerativeModel(self.model_name)
        return self._model

    def set_profile(self, config: Dict[str, Any]):
        """Set the candidate profile from config."""
        self.config = config
        # Invalidate cached objects that depend on config
        self._option_matcher = None
        self._config_sub_dicts = None
        profile_parts = []

        if "personal_info" in config:
            pi = config["personal_info"]
            profile_parts.append(f"Name: {pi.get('first_name', '')} {pi.get('last_name', '')}")
            if pi.get("linkedin"):
                profile_parts.append(f"LinkedIn: {pi['linkedin']}")
            if pi.get("github"):
                profile_parts.append(f"GitHub: {pi['github']}")

        if "education" in config and config["education"]:
            edu = config["education"][0]
            profile_parts.append(
                f"Education: {edu.get('degree', '')} in {edu.get('field_of_study', '')} "
                f"from {edu.get('school', '')}, graduating {edu.get('graduation_date', '')}"
            )
            if edu.get("gpa"):
                profile_parts.append(f"GPA: {edu['gpa']}")

        if "skills" in config:
            skills = config["skills"]
            if skills.get("programming_languages"):
                langs = [l["name"] if isinstance(l, dict) else l for l in skills["programming_languages"]]
                profile_parts.append(f"Programming: {', '.join(langs[:5])}")
            if skills.get("frameworks"):
                profile_parts.append(f"Frameworks: {', '.join(skills['frameworks'][:5])}")

        if "experience" in config and config["experience"]:
            exp = config["experience"][0]
            profile_parts.append(f"Most Recent Experience: {exp.get('title', '')} at {exp.get('company', '')}")

        self.profile_str = "\n".join(profile_parts)

    def set_job_context(self, company: str, role: str, description: str = "", ats_type: str = ""):
        """Set the current job being applied to."""
        self.job_context = {
            "company": company,
            "role": role,
            "description": description[:500] if description else "",
            "ats_type": ats_type,
        }

    def _get_grad_date(self) -> str:
        """Get graduation date from config. Delegates to OptionMatcher (single source of truth)."""
        return self._get_option_matcher()._get_grad_date()

    def _get_internship_term(self) -> str:
        """Get current internship term. Delegates to OptionMatcher (single source of truth)."""
        return self._get_option_matcher()._get_internship_term()

    def _get_language_years(self, tech: str) -> Optional[str]:
        """Look up years of experience for a specific language/technology from config."""
        skills = self.config.get("skills", {})
        langs = skills.get("programming_languages", [])
        tech_lower = tech.lower().strip()

        # Check programming_languages list
        for lang in langs:
            if isinstance(lang, dict):
                name = lang.get("name", "").lower()
                if tech_lower in name or name in tech_lower:
                    return str(lang.get("years", "1"))

        # Check frameworks/tools lists
        frameworks = [f.lower() if isinstance(f, str) else f for f in skills.get("frameworks", [])]
        tools = [t.lower() if isinstance(t, str) else t for t in skills.get("tools", [])]

        for f in frameworks:
            if tech_lower in f or f in tech_lower:
                return skills.get("years_of_coding_experience", "2")
        for t in tools:
            if tech_lower in t or t in tech_lower:
                return skills.get("years_of_coding_experience", "2")

        # Technology not in config — return "0" so it's honest
        return "0"

    def _get_cached_config_dicts(self):
        """Cache config sub-dicts to avoid re-extracting on every _get_config_answer call."""
        if not hasattr(self, '_config_sub_dicts') or self._config_sub_dicts is None:
            self._config_sub_dicts = {
                "common": self.config.get("common_answers", {}),
                "personal": self.config.get("personal_info", {}),
                "work_auth": self.config.get("work_authorization", {}),
                "screening": self.config.get("screening", {}),
                "availability": self.config.get("availability", {}),
                "skills": self.config.get("skills", {}),
                "education": self.config.get("education", [{}])[0] if self.config.get("education") else {},
                "experience": self.config.get("experience", [{}])[0] if self.config.get("experience") else {},
                "demographics": self.config.get("demographics", {}),
            }
        return self._config_sub_dicts

    def _get_config_answer(self, question: str, field_type: str = "text") -> Optional[str]:
        """
        Try to answer from config without AI.
        Returns None if no matching config answer found.
        """
        q = question.lower().strip()
        cd = self._get_cached_config_dicts()
        common = cd["common"]
        personal = cd["personal"]
        work_auth = cd["work_auth"]
        screening = cd["screening"]
        availability = cd["availability"]
        skills = cd["skills"]
        education = cd["education"]
        experience = cd["experience"]
        demographics = cd["demographics"]

        # Pattern matching for common questions
        patterns = [
            # LinkedIn URL - MUST be before generic patterns
            (r"^linkedin\s*(profile|url)?\*?$",
             personal.get("linkedin", "")),
            (r"(linkedin)\s*(profile|url|page)?\s*(url)?",
             personal.get("linkedin", "")),
            (r"(your|provide|enter)\s*linkedin",
             personal.get("linkedin", "")),

            # GitHub URL
            (r"^github\s*(profile|url)?\*?$",
             personal.get("github", "")),
            (r"(github)\s*(profile|url|page)?\s*(url)?",
             personal.get("github", "")),
            (r"(your|provide|enter)\s*github",
             personal.get("github", "")),

            # Website / Portfolio URL
            (r"^(website|portfolio)\s*(url)?\*?$",
             personal.get("portfolio", "") or personal.get("website", "")),
            (r"(website|portfolio|personal\s*site)\s*(url)?",
             personal.get("portfolio", "") or personal.get("website", "")),
            (r"(your|provide|enter)\s*(website|portfolio)",
             personal.get("portfolio", "") or personal.get("website", "")),

            # Full name
            (r"^(full\s*name|name)\*?$",
             personal.get("full_name", f"{personal.get('first_name', '')} {personal.get('last_name', '')}").strip()),

            # First name standalone
            (r"^first\s*name\*?$",
             personal.get("first_name", "")),

            # Last name standalone
            (r"^last\s*name\*?$",
             personal.get("last_name", "")),

            # Email standalone
            (r"^email\s*(address)?\*?$",
             personal.get("email", "")),

            # Phone standalone
            (r"^phone\s*(number)?\*?$",
             personal.get("phone", "")),

            # Address
            (r"^(address|street\s*address)\*?$",
             personal.get("address", "")),

            # Zip/Postal code
            (r"^(zip|postal)\s*(code)?\*?$",
             personal.get("zip_code", "")),

            # Country
            (r"^country\*?$",
             personal.get("country", "United States")),

            # Why interested questions
            (r"(why|what).*(interest|excite|intrigue|attract|appeal|draw|motivat).*(company|role|position|job|us|here|this|being|engineer|developer|analyst|scientist)",
             common.get("why_interested_in_company") or common.get("why_interested_in_role") or
             f"I'm drawn to this role because it aligns with my hands-on experience building data pipelines and "
             f"full-stack applications. At Kruiz, I ingested 831K+ records into PostgreSQL, built RAG search with "
             f"pgvector and Vertex AI, and led scraping pipelines across 7 hotel chains. I'm eager to apply these "
             f"skills at {self.job_context.get('company', 'your company')} and tackle real-world engineering challenges."),

            # Tell me about yourself
            (r"(tell|describe).*(about yourself|yourself)",
             common.get("tell_me_about_yourself") or
             f"I'm a {education.get('degree', 'student')} in {education.get('field_of_study', 'Computer Science')} "
             f"at {education.get('school', 'university')}, graduating {education.get('graduation_date', 'soon')}. "
             f"I have experience with {', '.join(skills.get('frameworks', [])[:3])} and I'm passionate about building software."),

            # Greatest strength
            (r"(greatest|biggest|main|top).*(strength|skill|asset)",
             common.get("greatest_strength") or
             "I'm a fast learner who can quickly adapt to new technologies and codebases. "
             "I also have strong problem-solving skills developed through coursework and projects."),

            # Greatest weakness
            (r"(greatest|biggest|main).*(weakness|improve|develop)",
             common.get("greatest_weakness") or
             "I sometimes focus too deeply on perfecting details. I'm actively working on balancing "
             "thoroughness with efficiency and meeting deadlines."),

            # Where do you see yourself
            (r"where.*(see yourself|5 year|five year|future)",
             common.get("where_see_yourself_5_years") or
             "In five years, I see myself as a senior engineer with deep technical expertise, "
             "potentially mentoring junior developers and contributing to architectural decisions."),

            # Project you're proud of / recent accomplishment
            (r"(project|accomplish|proud|recent work|built|build|created)",
             common.get("proud_project")),

            # Why should we hire you / what makes you a good fit
            (r"(why.*(hire|choose|pick|select)|good fit|what makes you|stand out|bring to)",
             common.get("why_hire_you")),

            # Teamwork / collaboration — must ask about teamwork experience, not just mention "team"
            # "collaborat" must be followed by experience words, not location (e.g., "collaboration in Boston")
            (r"(teamwork|team.*(experience|example|describe|tell)|collaborat.*(experience|example|describe|tell|skill)|work.*(with others|together)|group project)",
             common.get("teamwork_example")),

            # Challenge / problem solving / obstacle
            (r"(challenge|difficult|obstacle|problem.*(solv|overc)|overcame|struggled)",
             common.get("challenge_overcome")),

            # What field/area for internship — return major
            (r"(what|which).*(field|area|discipline).*(internship|intern|co.?op|looking)",
             education.get("major", "Software Engineering")),

            # Career interests / areas of engineering
            (r"(career|area|field).*(interest|excite|passion|focus)",
             common.get("career_interests")),

            # Outstanding offers / deadlines
            (r"(outstanding|other|competing).*(offer|deadline|application)",
             common.get("outstanding_offers") or "No"),

            # Languages spoken
            (r"(language|speak|fluent|bilingual|multilingual)",
             common.get("languages_spoken") or "English"),

            # Bilingual yes/no questions (Korean/English, Spanish/English, etc.)
            (r"(bilingual|fluent|proficient|speak).*(korean|japanese|mandarin|chinese|spanish|french|german|portuguese)",
             "No"),
            (r"(korean|japanese|mandarin|chinese|spanish|french|german|portuguese).*(bilingual|fluent|proficient|speak)",
             "No"),

            # Recruiting events / career fairs
            (r"(attend|visit|participate).*(recruit|career|fair|event|campus)",
             common.get("attended_recruiting_events") or "No"),

            # Preferred name
            (r"^preferred\s*name\*?$",
             personal.get("preferred_name") or personal.get("first_name", "Alan")),

            # What is your degree / degree type / what type of degree pursuing
            (r"^what\s*is\s*your\s*(degree|diploma)",
             education.get("degree", "Bachelor of Science")),
            (r"(type|kind|level).*(degree|diploma).*(pursuing|seeking|enrolled|working)",
             education.get("degree", "Bachelor of Science")),
            (r"(degree|diploma).*(type|kind|level|pursuing|seeking|current)",
             education.get("degree", "Bachelor of Science")),

            # Highest level of education
            (r"(highest|level).*(education|degree|academic)",
             education.get("degree", "Bachelor's degree")),

            # High school name
            (r"high\s*school",
             "Evergreen Valley High School"),

            # Are you a US citizen (standalone)
            (r"(citizen|citizenship|nationality)",
             "Yes" if work_auth.get("us_citizen", True) else "No"),

            # Available for summer internship
            (r"available.*(summer|fall|spring|winter)?\s*(internship|intern|co.?op|position)",
             "Yes"),

            # Gender (standalone, not in a label context)
            (r"^gender\*?$",
             demographics.get("gender", "Male")),

            # Hispanic/Latino
            (r"hispanic|latino|latina|latinx",
             "No"),

            # Race/Ethnicity (standalone)
            (r"^(race|ethnicity|racial).*$",
             demographics.get("ethnicity", "Asian")),

            # Current title / job title
            (r"(current|most recent|present)\s*(title|role|position|job)",
             experience.get("title") or "Data Engineer"),

            # Referral name (standalone — no referral)
            (r"^referral\s*(name|employee)?\*?$",
             "N/A"),

            # Drivers license
            (r"driver.?s?\s*(license|licence|permit)",
             "Yes"),

            # "If you selected other" / "please specify" follow-ups
            (r"(if you selected|please specify|please type).*(other|above|answer)",
             common.get("how_did_you_hear") or "Online Job Board"),

            # Additional information / anything else to share
            (r"(additional\s+information|anything else|other information|more.*share|like to add|like to share|anything.*add)",
             common.get("additional_information")),

            # Experience with technology / describe your experience
            (r"(describe|tell).*(experience|background).*(with|in|using)?\s*(technology|programming|coding|software|engineer)?",
             common.get("experience_with_technology")),

            # Preferred programming language
            (r"(prefer|favorite|primary|go.?to).*(programming|coding)?\s*(language)",
             common.get("preferred_programming_language") or "Python"),

            # Code sample / project link
            (r"(code.?sample|project.*link|link.*project|portfolio.*link|work.*sample|code.*link)",
             common.get("code_sample_link") or personal.get("github", "")),

            # How did you hear about us
            (r"(how|where).*(hear|find|learn|discover).*(about|position|role|us|job|opportunity)",
             common.get("how_did_you_hear") or "LinkedIn"),

            # Hourly vs salary position preference
            (r"(desire|prefer|want).*(hourly|salary)\s*(or|position|pay)",
             "Hourly"),
            (r"(hourly|salary)\s*(or|vs)\s*(hourly|salary)\s*(position|pay)?",
             "Hourly"),

            # Type of employment desired
            (r"type of employment desired",
             "Full-time"),
            (r"type of (employment|work|position).*(desired|seeking|looking|prefer)",
             "Full-time"),

            # Date available for work / start date
            (r"date\s*(available|you.*(can|could|are).*(start|begin|available))\s*(for work|to work|to start)?",
             availability.get("earliest_start_date") or "05/19/2026"),

            # Generic "From" / "To" date fields (work experience / education)
            # "To (Actual or Expected)" — graduation or end date
            (r"^to\s*\(actual\s*(or|/)\s*expected\)",
             education.get("graduation_date", "May 2026")),
            # Bare "To" — end date of most recent experience or education graduation
            (r"^to\s*\*?\s*$",
             experience.get("end_date", "") or education.get("graduation_date", "May 2026")),
            # Bare "From" — start date of most recent experience or education
            (r"^from\s*\*?\s*$",
             experience.get("start_date", "October 2025") if experience else
             education.get("start_date", "August 2021")),

            # Days and times available to work
            (r"(list|what).*(days|times).*(available|can).*(work|you)",
             "Monday through Friday, 8:00 AM - 5:00 PM"),
            (r"days and times.*(available|work)",
             "Monday through Friday, 8:00 AM - 5:00 PM"),

            # Desired hourly rate
            (r"desired hourly rate",
             "Open to discuss"),

            # Current base salary / hourly rate (we're a student)
            (r"(current|what is your).*(base\s*salary|hourly|pay\s*rate|compensation)",
             "N/A"),

            # Salary expectations
            (r"(salary|compensation|pay|wage).*(expect|requirement|range|desire|target|looking for)",
             common.get("salary_expectations") or "Open to discuss based on the role requirements"),
            (r"target.*(salary|compensation|pay)",
             common.get("salary_expectations") or "Open to discuss based on the role requirements"),

            # "Are you able to meet the dates/requirements" → Yes (must be BEFORE start date)
            (r"(are you able to|can you (meet|commit|attend)|able to (meet|commit|attend)).*(date|schedule|requirement|program)",
             "Yes"),

            # "Program begins on X and wraps on Y" — commitment to dates → Yes
            (r"(program|internship).*(begins|starts).*(and|wraps|ends|through|until)",
             "Yes"),

            # Start date / availability — MUST be about starting work, not generic "when" or "start date" in context
            (r"(when can you (start|begin)|earliest.*(start|begin|available)|available to start|availability.*(start|begin|date)|when.*(start|begin).*(work|role|position|intern))",
             availability.get("earliest_start_date") or availability.get("preferred_start_date") or "Immediately"),

            # Years of experience - coding
            (r"(year|how long|how many).*(coding|programming|software|development).*(experience)",
             skills.get("years_of_coding_experience") or "3"),

            # Years of experience - professional
            (r"(year|how long|how many).*(professional|work|industry).*(experience)",
             skills.get("years_of_professional_experience") or "1"),

            # "Do you have X years of non-internship/industry experience?" type questions
            # These appear as yes/no dropdowns asking about minimum experience thresholds
            (r"(have|do you have|do you).*(2|two|3|three|4|four|5|five|\d+).*(year|yr).*(industry|non.?intern|professional|work).*(experience)",
             "No"),
            (r"(industry|non.?intern|professional|work).*(experience).*(2|two|3|three|4|four|5|five|\d+).*(year|yr)",
             "No"),

            # Number of prior internships / co-ops
            (r"how many.*(internship|co.?op|intern)",
             skills.get("num_prior_internships") or "1"),

            # GPA threshold yes/no questions (e.g. "Is your GPA 3.0 or higher?")
            (r"(gpa|grade.*point).*(3\.0|3\.5|2\.5|minimum|or higher|at least|above)",
             "Yes"),
            (r"(is|do you have).*(gpa|grade.*point).*(3|above|higher|minimum)",
             "Yes"),

            # Built/deployed a project with Python/JavaScript/AI questions
            (r"(built|deployed|created).*(project|app|application).*(python|javascript|js|ai|ml|api|llm|integrat)",
             "Yes"),
            (r"(project|app|application).*(python|javascript|js|ai|ml|api|integrat).*(built|deployed|created)",
             "Yes"),

            # GPA (including "G. P. A." format)
            (r"(gpa|g\.?\s*p\.?\s*a|grade point|cumulative|academic.*gpa)",
             str(education.get("gpa", "")) if education.get("gpa") else None),

            # Currently enrolled / pursuing degree — MUST be before graduation pattern
            # Matches: "are you currently enrolled", "are you currently pursuing a degree"
            # BUT NOT "which university are you currently attending" (that's a school-name question)
            # Graduation date / when slated to graduate / expected graduation
            # MUST come BEFORE enrolled patterns — "if currently enrolled...anticipated graduation date"
            # is asking for a date, not enrollment status.
            (r"(graduation|completion).*(date|month|year)",
             education.get("graduation_date") or self._get_grad_date()),
            (r"(graduation date|expected graduat|when.*graduat|when.*finish|complet.*degree|slated.*grad|anticipated.*graduat)",
             education.get("graduation_date") or self._get_grad_date()),
            # "If currently enrolled...write-in your anticipated graduation/completion date"
            (r"(currently|presently).*(enroll|student).*(graduation|completion|anticipated).*(date|month|year)",
             education.get("graduation_date") or self._get_grad_date()),

            # Currently pursuing a degree/program (enrollment status — yes/no)
            # Narrowed: requires degree/program/school context to avoid matching graduation date questions
            (r"(currently|presently).*(pursuing|enroll|student|study).*(degree|program|school|education|undergrad|graduate)",
             "Yes"),

            # Enrolled catch-all (after graduation date patterns)
            # Note: "attend" removed — "which university are you currently attending" is a school-name question
            (r"(currently|presently).*(enroll|student|study|pursuing)",
             "Yes"),

            # Internship term / when to start internship
            (r"(internship|intern).*(term|session|period|start|begin)",
             self._get_internship_term()),
            (r"(what|which).*(term|time|date).*(start|begin).*(internship|intern)",
             self._get_internship_term()),

            # "Do you currently reside within the continental United States?"
            (r"(reside|live|located).*(continental|united states|u\.s)",
             "Yes"),

            # "Are you actively completing your Ph.D.?"
            (r"(ph\.?d|phd|doctoral)",
             "No"),

            # "Please confirm the season you are applying for"
            (r"(season|term).*(applying|apply|interested|intern|confirm)",
             self._get_internship_term()),

            # "I have read and understand [privacy notice/policy]"
            (r"(i have read|read and understand).*(privacy|notice|policy|terms)",
             "Yes"),

            # Commuting distance / willing to relocate / onsite requirement — MUST be before "current location"
            (r"(commut|relocat|willing to|are you ok|on-?site|hybrid|in.?person).*(distance|move|relocat|requirement|campus|office|week)",
             "Yes"),

            # Position is based in / requires being in [city] — answer Yes
            # NOTE: must NOT match "required to obtain...sponsorship" — require location words
            (r"(position|role|job|this).*(based in|based out|located in|located at|requires being in|out of our|work from)",
             "Yes"),

            # Able to meet required dates / program dates
            (r"(able|available).*(meet|attend|start).*(required|program|date)",
             "Yes"),

            # "Which available location(s) on the posting are you open to?" — answer with city, state
            (r"(available|which).*(location|city|office).*(posting|open|interest|prefer)",
             f"{personal.get('city', '')}, {personal.get('state', '')}".strip(", ")),

            # Current location / where are you located
            (r"(current|where).*(locat|city|based|live|reside)",
             f"{personal.get('city', '')}, {personal.get('state', '')}".strip(", ")),

            # What is your major / field of study
            (r"(what|your).*(major|field of study|area of study|concentration|discipline)",
             education.get("field_of_study") or "Computer Science"),

            # Which college/university do you attend
            (r"(which|what|your).*(college|university|school|institution).*(attend|enroll|study)",
             education.get("school") or "University"),

            # Type of academic program (undergrad, grad, etc.)
            (r"(type|kind).*(academic|degree).*(program)",
             "Undergraduate" if "bachelor" in str(education.get("degree", "")).lower() else "Graduate"),

            # Visa status / work permit type
            (r"(current|what).*(visa|immigration|work permit|work authorization).*(status|type)",
             work_auth.get("current_visa_status") or ("Citizen" if work_auth.get("us_citizen") else "N/A")),

            # Export control: list countries of citizenship
            (r"(export control|export licensing|list.*(countr|citizen|permanent resident))",
             "United States — citizen since birth"),

            # Pronouns
            (r"(prefer|your).*(pronoun)",
             self.config.get("demographics", {}).get("pronouns") or ""),

            # Race/ethnicity (text field version)
            (r"(race|ethnic|background).*(select|describe|identify)",
             self.config.get("demographics", {}).get("ethnicity") or "Prefer not to say"),

            # Gender (text field version)
            (r"(gender).*(select|describe|identify)",
             self.config.get("demographics", {}).get("gender") or "Prefer not to say"),

            # Veteran status (text field version)
            (r"veteran.*(status|u\.s\.|military)",
             "I am not a protected veteran"),

            # Disability (text field version)
            (r"disab.*(status|identify|condition)",
             "No, I don't have a disability"),

            # UK diversity monitoring questions (mthree, Peregrine, UK employers)
            # Country of current residence
            (r"country.*(current|your|of).*(residence|residing|live|based)",
             personal.get("country", "United States")),
            (r"country.*residence",
             personal.get("country", "United States")),

            # Racial/ethnic groups (UK)
            (r"racial.*(ethnic|group|describe|identify)",
             "Prefer not to say"),
            (r"ethnic.*(group|background|describe|racial)",
             "Prefer not to say"),

            # Impairment / health condition (UK disability monitoring)
            (r"(impairment|long.?term).*(health|condition|consider|have)",
             "No"),
            (r"consider.*(yourself|have).*(impairment|disability|health condition)",
             "No"),

            # Neurodiverse condition
            (r"neurodiverse|neurodiversity|neurodivers",
             "No"),
            (r"(adhd|autism|dyspraxia|dyslexia).*(condition|diagnos|have)",
             "No"),

            # UK school type (socioeconomic)
            (r"type.*(school|secondary).*(attend|11|16|ages)",
             "Prefer not to say"),
            (r"school.*(attend|type).*(11|16|secondary|ages)",
             "Prefer not to say"),

            # Free school meals (UK socioeconomic)
            (r"free.*(school|meals|lunch)",
             "No"),
            (r"eligible.*(free.*school|school.*meals)",
             "No"),

            # Parents university degree (UK socioeconomic)
            (r"(parent|guardian).*(university|degree|higher.*education|attend.*univ)",
             "Yes"),
            (r"(university|degree).*(parent|guardian|mother|father)",
             "Yes"),

            # Household earner occupation at age 14 (UK socioeconomic)
            (r"occupation.*(household|earner|parent|guardian).*(14|age|when you were)",
             "Prefer not to say"),
            (r"(household|earner|main earner).*(occupation|job|work).*(14|age)",
             "Prefer not to say"),

            # UK employment status / location questions
            # MUST NOT match "sponsorship for employment visa status"
            (r"^(?!.*sponsor).*employment\s+(status|category|type)",
             "Full-time student"),
            (r"(please indicate|indicate|describe).*(employment|working|work status|currently)",
             "Full-time student"),
            (r"(currently|where).*(located|based|living).*(united kingdom|UK|england|scotland|wales|northern ireland)",
             "Not Applicable"),
            (r"(united kingdom|UK).*(currently|located|based|living|where)",
             "Not Applicable"),

            # Company name (current/previous employer)
            (r"^company\s*name\*?$",
             experience.get("company", "N/A") if experience else "N/A"),
            (r"(current|most recent|previous|last)\s+(employer|company)\b",
             experience.get("company", "N/A") if experience else "N/A"),

            # City (standalone question)
            (r"^city\*?$",
             personal.get("city", "San Francisco")),

            # State (standalone question)
            (r"^state\*?$",
             personal.get("state", "CA")),

            # Timezone
            (r"(time.?zone|timezone)",
             availability.get("timezone", "America/Los_Angeles") or "Pacific Time (PT)"),

            # Current year in school (dynamic from graduation date)
            (r"(current|what).*(year).*(school|program|study|academic)",
             "Senior"),  # Based on graduating May 2026

            # Pay/salary comfortable
            (r"(comfortable|agree|okay).*(pay|salary|compensation|guideline)",
             "Yes"),

            # University recruiting events
            (r"(attend|visit|participate).*(recruiting|career|fair|event|university)",
             "No"),

            # Work sector/industry experience
            (r"(work|experience).*(sector|industry|energy|tech)",
             "No"),

            # Referred by employee
            (r"(referred|referral).*(employee|someone|staff|name)",
             ""),  # Empty or "N/A" for no referral

            # Company careers page (referral source field showing page name)
            (r"careers?\s*page",
             "Online Job Board"),

            # Referral name
            (r"(name of|referral).*(employee|referrer|person)",
             "N/A"),

            # "If yes, specify name and relationship" (conditional — only appears when relatives=Yes)
            (r"if\s*yes.*specify.*name.*relationship",
             "N/A"),
            (r"specify.*name.*relationship",
             "N/A"),

            # Conviction details (conditional — only appears when conviction=Yes)
            (r"(provide|give|describe).*details.*(conviction|felony|misdemeanor|crime)",
             "N/A"),
            (r"(details|explain).*(conviction|charge|offense|arrest)",
             "N/A"),

            # Educational institution name/city/state listing
            (r"(list|provide).*(educational institution|institution name|school name).*(city|state|name)",
             "San Jose State University, San Jose, CA"),

            # Emergency contact name
            (r"emergency.*contact.*(name)?",
             "N/A"),

            # Languages spoken
            (r"(language|speak).*fluent",
             "English"),

            # Legally blind or deaf
            (r"(blind|deaf|hearing|vision).*(impair|disab)",
             "No"),

            # Know anyone who works at [company] — MUST be before "currently employed"
            (r"(know|acquaint).*(anyone|someone|of anyone).*(work|employ|at)",
             False),

            # Are you currently employed
            (r"(are you|do you).*(currently|presently).*(employed|working)",
             "No" if not experience else "Yes"),

            # Notice period / how soon can you start
            (r"(notice|period|how soon).*(give|start|begin)",
             availability.get("notice_period") or "Immediately"),

            # Travel percentage
            (r"(travel|willing to travel).*(percent|%)",
             screening.get("travel_percentage", "25")),

            # Expected graduation year (just year)
            (r"(expected|anticipated).*(graduation|grad).*(year)",
             str(education.get("graduation_date", self._get_grad_date()))[-4:]),

            # Sponsorship requirement (standalone label)
            (r"^sponsorship\s*(requirement)?\*?$",
             "No" if not work_auth.get("require_sponsorship_now", False) else "Yes"),

            # What type of degree are you pursuing
            (r"(type|kind).*(degree).*(pursuing|studying|working|earning|completing)",
             education.get("degree", "Bachelor of Science")),

            # Bare degree option passed as question (Lever bug)
            (r"^bachelor'?s?(\s*(degree|of))?\.?$",
             education.get("degree", "Bachelor of Science")),

            # Bare "Yes" / "No" passed as question — return as-is
            (r"^(yes|no)[\.\s]*$",
             None),  # Skip — don't fill garbage

            # City/location names passed as questions (dropdown option text leak)
            (r"^[A-Z][a-z]+,\s*[A-Z]{2},?\s*(United States|Canada|USA|UK)?[\.\s]*$",
             None),  # Skip — this is a dropdown option, not a question

            # Pronouns — He/him, She/her, They/them
            (r"(he/?him|she/?her|they/?them|pronouns)",
             personal.get("pronouns", "He/him")),

            # Website standalone label
            (r"^website\*?$",
             personal.get("portfolio", "") or personal.get("github", "")),

            # Twitter / X URL — N/A (we don't have one)
            (r"(twitter|^x\s*url|^x$)\s*(url|profile)?",
             "N/A"),

            # Portfolio URL / Other website — return empty or portfolio
            (r"(portfolio|other\s*website|personal\s*website)\s*(url)?",
             personal.get("portfolio", "")),

            # Transcript URL
            (r"transcript", "N/A"),

            # Other URL (catch-all for unrecognized URL fields)
            (r"^other\s*(url)?\*?$", "N/A"),

            # Which university are you currently attending?
            (r"(which|what).*(university|school|college).*(attend|enroll|current)",
             education.get("school", "San Jose State University")),
            # "Which university" without "attending" — still means current school
            (r"(which|what).*(university|school|college)",
             education.get("school", "San Jose State University")),

            # Previously worked for / at [company]
            (r"(previous|have you).*(work|employ).*(for|at)\s",
             "No"),

            # Family members who are physicians / doctors
            (r"(family|immediate).*(member|relative).*(physician|doctor|practic)",
             "No"),

            # Debarred by government
            (r"(debarred|excluded|sanction)",
             "No"),

            # Sanctioned countries citizen (Cuba, Iran, North Korea, Syria)
            (r"(citizen|resident).*(cuba|iran|north korea|syria|crimea)",
             "No"),

            # "If answered Yes, provide name" follow-ups → N/A
            (r"(if answered yes|if yes|if so).*(provide|name|describe|explain)",
             "N/A"),

            # Know anyone at [company] / do you know anyone who works at
            (r"(know).*(anyone|someone|somebody|of anyone).*(work|at|company|employ)",
             "No"),
            (r"(do you).*(currently)?\s*(know)",
             "No"),
            # "Do you currently know of anyone who works at X? If so, provide name"
            (r"(know of anyone|know anyone).*(work)",
             "No"),

            # Confirm state of residence during internship
            (r"(confirm|state).*(reside|live|located).*(during|internship)",
             personal.get("state", "CA")),

            # Do you have experience with [technology]?
            (r"(do you have|have you).*(experience|proficien|familiar|work).*(with|in|using)\s+",
             "Yes"),

            # How many years of experience with [specific language/technology]
            (r"how many years.*(experience|work).*(with|in|using)\s+(\w+)",
             None),  # Handled by _get_language_years below

            # Cover letter text (if no file)
            (r"cover.?letter",
             common.get("cover_letter_text", "Please see my attached resume for details on my experience and qualifications.")),
        ]

        # Check text patterns
        for pattern, answer in patterns:
            if answer is not None and re.search(pattern, q):
                if answer == "__SKIP__":
                    # Explicitly skip this question (don't fill it)
                    logger.info(f"Config pattern skip: '{question[:40]}...' -> skip (no military/govt)")
                    return "__SKIP__"
                # Convert booleans to Yes/No strings
                if isinstance(answer, bool):
                    answer = "Yes" if answer else "No"
                logger.info(f"Config fallback matched: '{question[:40]}...' -> pattern match")
                return str(answer).strip()

        # Special handler: "How many years of experience with [language]?"
        lang_match = re.search(r"how many years.*(experience|work).*(with|in|using)\s+(.+?)[\*\?\.]*$", q)
        if lang_match:
            tech = lang_match.group(3).strip().rstrip("*?. ")
            years = self._get_language_years(tech)
            if years:
                logger.info(f"Config fallback matched: '{question[:40]}...' -> {years} years")
                return years

        # Yes/No question patterns
        yes_no_patterns = [
            # Work authorization - including "unrestricted right"
            (r"(authorized|eligible|legally|unrestricted).*(work|employ|right).*(us|united states|u\.s)",
             work_auth.get("us_work_authorized", True)),
            (r"(citizen|permanent resident).*(us|united states|u\.s)",
             work_auth.get("us_citizen", False) or work_auth.get("us_permanent_resident", False)),
            (r"(require|need|will you|would you|if hired).*(sponsor|visa|h1b|h-1b|now or in the future|employment authorization|immigration)",
             work_auth.get("require_sponsorship_now", False) or work_auth.get("require_sponsorship_future", False)),
            # ITAR / export compliance asking about needing sponsorship
            (r"(itar|export).*(require|need|sponsor|additional)", False),
            # Export compliance: citizen/resident of Cuba, Iran, North Korea, Syria, Crimea
            (r"(citizen|resident|national origin).*(cuba|iran|north korea|syria|crimea)", False),

            # Military service — "have you ever served in the military"
            (r"(served|serving|service).*(military|armed forces|us army|us navy|us air force)",
             False),  # Not a veteran
            (r"(military|armed forces).*(served|service|veteran|member)",
             False),
            # Military/Government status follow-up fields — skip if no service
            # "Current Military Status" with options like Active/Retired/Other
            (r"current military status", "__SKIP__"),
            (r"military (dates? of service|branch|rank)", "__SKIP__"),
            (r"terminal leave date", "__SKIP__"),
            (r"current government status", "__SKIP__"),
            (r"government (start|end|separation) date", "__SKIP__"),
            (r"government separation", "__SKIP__"),
            (r"transition leave date", "__SKIP__"),
            (r"military service.*(from|to|start|end)\b", "__SKIP__"),
            # Government employee/service — "served as a government employee"
            (r"(served|serving).*(government|federal|state).*(employee|official|capacity)", False),
            (r"(government|federal).*(employee|service|official|capacity)", False),

            # Previously worked/employed/applied at company
            (r"(previously|have you|ever).*(worked|employed|applied|been an? .* employee|been an employee).*(for|at|with|work)?", False),
            # Family member at company
            (r"(family member|relative).*(work|employ)", False),
            # Commitments to another employer
            (r"(commitment|obligation).*(employer|organization|company|interfere)", False),

            # Age
            (r"(18|eighteen).*(year|age|older)", screening.get("is_18_or_older", True)),
            (r"(21|twenty.?one).*(year|age|older)", screening.get("is_21_or_older", True)),
            (r"(legal age|of age|over the age|at least 18|age of 18)", screening.get("is_18_or_older", True)),

            # Relocation
            (r"(willing|able|open|required|need).*(relocat|move)", availability.get("willing_to_relocate", True)),

            # Background check
            (r"(background|criminal).*(check|screen)", screening.get("can_pass_background_check", True)),
            (r"(consent|willing|agree).*(background check)", screening.get("agree_to_background_check", True)),
            (r"(drug).*(test|screen)", screening.get("can_pass_drug_test", True)),
            # Criminal history / felony conviction
            (r"(convicted|felony|misdemeanor|crime|criminal)", screening.get("has_criminal_record", False)),

            # Tobacco / smoking
            (r"(tobacco|smok|nicotine|vape|cigarette)", False),

            # Travel
            (r"(willing|able).*(travel)", screening.get("willing_to_travel", True)),

            # Managing direct reports (internship = no)
            (r"(managing|manage|supervise).*(direct report|employee|staff|people)", False),

            # Employment history at this company
            (r"employment history", False),
            # Debarred by FDA / excluded by OIG
            (r"(debarred|excluded).*(fda|oig)", False),
            # AI usage policy acknowledgment
            (r"ai usage policy", True),

            # Agreement/consent/terms - these should be handled as checkboxes
            (r"(agree|consent|acknowledge|read and agree).*(term|policy|condition|privacy)", True),
            (r"(i have read|yes.*read)", True),

            # Current employee question
            (r"(are you|current).*(employee|employed).*(here|at|of|with|by)?",
             screening.get("is_current_employee", False)),

            # Previously applied/employed at ANY company
            (r"(previous|before|prior|have you).*(work|employ).*(for|at|here|this)",
             screening.get("previously_employed_here", False)),
            (r"(previous|before|prior|have you).*(appl).*(for|to|here|this|position)",
             screening.get("previously_applied_here", False)),

            # Is internship part of co-op requirement
            (r"(co.?op|coop).*(require|program|part of)", False),

            # Have you completed an internship before (past tense — NOT "complete your internship in [field]")
            (r"(completed|done|had|previous).*(internship|intern)",
             screening.get("has_prior_internship", True)),

            # Were you referred by an employee
            (r"(refer|referred by).*(current|employee|someone|anyone|staff)",
             screening.get("know_current_employees", False)),

            # Know employees / relatives employed
            (r"(know|relative|friend|family).*(work|employ|current).*(here|company)",
             screening.get("know_current_employees", False)),
            (r"(relative|family).*(employ|work)",
             screening.get("know_current_employees", False)),
            (r"(have any).*(relative|family|friend)",
             screening.get("know_current_employees", False)),
            # "Are you a relative or close friend of any [company]..."
            (r"(are you a|are you).*(relative|friend)",
             screening.get("know_current_employees", False)),

            # Full time / part time
            (r"(full.?time|40.?hour)", screening.get("can_work_full_time", True)),

            # Currently enrolled — handled in text patterns above (returns "Yes" string)
            # (kept here as backup for checkbox-type fields)
            # Note: "attend" removed — "which university are you currently attending" is a school-name question
            (r"(currently|presently).*(enroll|student|study|pursuing)", True),

            # Work onsite/hybrid/in-office questions
            (r"(able|willing|available).*(work|come).*(onsite|on-site|office|hybrid|in.?person)",
             availability.get("willing_to_relocate", True)),

            # Specific start date questions (Will you be able to start on X date)
            (r"(able|willing|available).*(start|begin).*(on|date|june|july|may|august|september)",
             True),

            # Work full schedule/hours questions
            (r"(able|willing|available).*(work|commit).*(\d+\s*(days|hours)|full.?time|through|until)",
             True),

            # Comfortable with work arrangement (onsite, hybrid, X days)
            (r"(comfortable|ok|okay).*(onsite|on-site|office|in.?person|hybrid|days|schedule|arrangement)",
             True),

            # This job requires X days a week onsite
            (r"(this|the).*(job|role|position).*(require|need).*(days|onsite|office|in.?person)",
             True),

            # Transportation / living accommodations for internship
            (r"(transportation|living accommodations|housing).*(duration|internship|summer|period)",
             True),

            # Location/office preference (all caps city names like ATLANTA, NEW YORK)
            (r"^[A-Z\s]+\*?$",  # All caps text that looks like a city
             True),

            # Able to work/live in specific location
            (r"(able|willing|can).*(work|live|be).*(in|at).*(location|city|office|atlanta|austin|new york|san francisco|seattle|chicago|boston|denver|los angeles)",
             availability.get("willing_to_relocate", True)),

            # Commuting distance
            (r"(commut|within).*(distance|drive|area|range)",
             availability.get("willing_to_relocate", True)),

            # Housing/stipend acknowledgments (role is based in X, no housing stipend, etc.)
            (r"(based|located|position).*(in|out of|at).*(office|headquarters|city|nyc|new york|san francisco|austin|seattle)",
             True),
            (r"(does not include|not provide|no).*(housing|stipend|relocation|assistance)",
             True),
            (r"(acknowledge|understand|aware|ok|okay).*(housing|stipend|relocation|based|location)",
             True),

            # Hourly pay / compensation acknowledgment
            (r"(hourly|pay|rate|compensation).*(is|\$|per hour)",
             True),
            (r"(comfortable|agree|accept|okay|ok).*(hourly|pay|rate|compensation|\$)",
             True),

            # Schedule/availability acknowledgments
            (r"(available|able|willing).*(work|be).*(10|12|40|full).*(week|hours)",
             True),

            # Internship term acknowledgments
            (r"(acknowledge|understand|confirm).*(internship|term|duration|dates)",
             True),

            # Previous experience questions
            (r"(have|had|previous).*(software|engineering|coding|development|internship).*(experience|intern)",
             True),

            # California residents disclosure / additional information
            (r"(california|ccpa).*(resident|disclosure|additional|information|notice)",
             True),
            (r"(additional information).*(california|ccpa)",
             True),

            # EEOC / voluntary self-identification acknowledgments
            (r"(voluntary|self.?identif|eeoc|disclosure).*(acknowledge|read|understand)",
             True),

            # Phone/text/SMS consent questions
            (r"(consent|agree).*(phone|sms|text|message|contact|call)",
             True),
            (r"(phone|sms|text).*(consent|agree|ok|okay)",
             True),

            # Email consent / communication consent
            (r"(consent|agree).*(email|communicat|contact|receiv)",
             True),

            # SMS / text message opt-in
            (r"(receive|opt).*(text|sms|message)",
             True),

            # "Would you like to receive information"
            (r"would you like to receive",
             True),

            # General consent questions
            (r"do you (consent|agree|authorize)",
             True),

            # Essential functions / reasonable accommodation
            (r"(perform|essential).*(function|duties).*(job|position|role)",
             True),
            (r"(reasonable accommodation|with or without)",
             True),

            # Non-compete / restrictive covenant / conflict of interest / outside business
            (r"(non.?compete|non.?solicitation|restrictive covenant|restrictive agreement)",
             False),
            (r"subject to.*(agreement|covenant|restriction|non)",
             False),
            (r"(conflict.*(interest|company)|outside.*(business|activit)|appearance.*conflict)",
             False),
            (r"(party to|bound by).*(employment|consulting|personal services).*(agreement|contract)",
             False),

            # Government/regulatory disqualification
            (r"(declared ineligible|disassociation|debarred|disqualif|prohibit.*contract)",
             False),
            (r"(subject.*specific authority|regulatory.*order|government.*sanction)",
             False),

            # Security clearance — "do you currently hold/have" → has_security_clearance (false)
            (r"(currently|do you).*(hold|have|possess).*(security clearance|clearance)",
             work_auth.get("has_security_clearance", False)),
            # Security clearance — "have you ever held" → has_security_clearance (false)
            (r"(ever|previously).*(held|had|possess).*(security clearance|clearance)",
             work_auth.get("has_security_clearance", False)),
            # Security clearance — "eligible/able/willing to obtain" → can_obtain_clearance (true)
            (r"(eligible|able|willing|obtain|get).*(security clearance|clearance|dod|department of defense)",
             work_auth.get("can_obtain_clearance", True)),
            # Generic security clearance — only for "can/able/willing" contexts
            (r"(can|able|willing).*(security clearance|clearance)",
             work_auth.get("can_obtain_clearance", True)),

            # Currently bound by agreements
            (r"(bound|subject).*(non.?compete|employment agreement|restrictive)",
             False),

            # "By submitting" consent / data processing
            (r"(by submitting|consent).*(application|data|information|process)",
             True),

            # Ready to start / can be in location by date
            (r"(can you be in|able to start|ready to start|available.*start).*(by|on|before)",
             True),

            # Considered for other openings
            (r"(considered|interested).*(other|additional).*(opening|position|role|opportunit)",
             True),

            # Previously employed at specific company
            (r"(previously|have you).*(worked|employed|been employed).*(at|by|for|with).*(\?|$)",
             False),
            (r"(have you been|were you).*(employed|worked).*(with|at|by|for)",
             False),
            (r"been employed with",
             False),

            # Over 18 / age requirement
            (r"(over|at least|above|are you).*(18|21|age|legal age|of age)",
             True),
            (r"(18|21).*(years|year).*(old|age|or older)",
             True),

            # Export control / ITAR / defense articles — affirm eligibility (US citizen)
            (r"(export control|itar|ear|defense article|defense service|import defense)",
             True),
            (r"(declared ineligible|denied.*export|denied.*license).*(contract|import|export|defense)",
             False),

            # "By selecting Yes" certification questions
            (r"by selecting.*(yes|agree|submit)",
             True),
            (r"certif.*(best of my knowledge|information.*true|information.*correct|information.*accurate)",
             True),

            # Acknowledge/confirm/agree catch-alls (broad — put last)
            (r"(acknowledge|confirm|certify).*(information|accurate|true|correct|complete)",
             True),
            (r"(acknowledge|confirm|agree).*(terms|conditions|policy|statement|notice|requirements)",
             True),

            # Bare "I acknowledge" — catch-all for acknowledgment checkboxes
            (r"^i\s*acknowledge",
             True),

            # Generic "Are you..." / "Do you..." / "Can you..." catch-all (very broad — absolute last)
            (r"^(are you|do you|can you|will you|would you).*(able|willing|available|interested|open|comfortable|ready)",
             True),
        ]

        for pattern, value in yes_no_patterns:
            if re.search(pattern, q):
                if value == "__SKIP__":
                    logger.info(f"Config pattern skip (yes_no): '{question[:40]}...' -> skip")
                    return "__SKIP__"
                answer = "Yes" if value else "No"
                logger.info(f"Config fallback (yes/no): '{question[:40]}...' -> {answer}")
                return answer

        return None

    async def answer_question(
        self,
        question: str,
        field_type: str = "text",
        options: Optional[list] = None,
        max_length: Optional[int] = None,
    ) -> str:
        """
        Generate an answer for a question.
        Priority: template bank → option matching → config patterns → cache → Gemini → fallback
        """
        # STEP 0: Per-template question bank (exact/fuzzy match from known questions)
        ats_type = self.job_context.get("ats_type", "")
        bank_result = self._lookup_template_bank(question, ats_type)
        if bank_result:
            answer = bank_result["answer"]
            # For dropdowns with options, verify the bank answer matches an actual option
            if options and field_type in ("dropdown", "select", "radio"):
                answer_lower = answer.lower().strip()
                option_match = any(
                    answer_lower == o.lower().strip() or
                    answer_lower in o.lower() or
                    o.lower().strip() in answer_lower
                    for o in options if o.strip()
                )
                if not option_match:
                    # Bank answer doesn't match any dropdown option — skip bank, use option matching
                    logger.info(f"Template bank answer '{answer[:30]}' doesn't match options {options[:5]} — falling through to option matching")
                    # Don't return — continue to next step
                else:
                    if max_length and len(answer) > max_length:
                        answer = answer[:max_length-3] + "..."
                    logger.info(f"Template bank hit ({ats_type}): '{question[:40]}...' -> '{answer[:40]}...'")
                    self.session_answers.append({"question": question, "answer": answer, "source": "template_bank"})
                    self._log_to_kb(question, answer, "template_bank", field_type=field_type)
                    return answer
            else:
                if max_length and len(answer) > max_length:
                    answer = answer[:max_length-3] + "..."
                logger.info(f"Template bank hit ({ats_type}): '{question[:40]}...' -> '{answer[:40]}...'")
                self.session_answers.append({"question": question, "answer": answer, "source": "template_bank"})
                self._log_to_kb(question, answer, "template_bank", field_type=field_type)
                return answer

        # STEP 1: For dropdowns with options, try option matching (knows actual menu items)
        if options and field_type in ("dropdown", "select", "radio"):
            matched = self._match_option_from_config(question, options)
            if matched:
                self.session_answers.append({"question": question, "answer": matched, "source": "config_option"})
                self._log_to_kb(question, matched, "config_option", field_type=field_type)
                self._auto_learn_to_template_bank(question, matched, field_type, "config_option")
                return matched

        # Try config-based answer
        config_answer = self._get_config_answer(question, field_type)
        if config_answer:
            if max_length and len(config_answer) > max_length:
                config_answer = config_answer[:max_length-3] + "..."
            self.session_answers.append({"question": question, "answer": config_answer, "source": "config"})
            self._log_to_kb(question, config_answer, "config", field_type=field_type)
            self._auto_learn_to_template_bank(question, config_answer, field_type, "config")
            return config_answer

        # Check verified answers database (human-approved)
        verified = self.verifier.get_verified_answer(question, field_type, options)
        if verified:
            answer = verified["answer"]
            if max_length and len(answer) > max_length:
                answer = answer[:max_length-3] + "..."
            logger.info(f"Verified answer: '{question[:40]}...' -> '{answer[:40]}...'")
            self.session_answers.append({"question": question, "answer": answer, "source": verified["source"]})
            self._log_to_kb(question, answer, verified["source"], field_type=field_type)
            self._auto_learn_to_template_bank(question, answer, field_type, "verified")
            return answer

        # Check answer cache before calling AI
        cache_key = self._cache_key(question, field_type, options)
        if cache_key in self._answer_cache:
            cached = self._sub_company(self._answer_cache[cache_key])
            if max_length and len(cached) > max_length:
                cached = cached[:max_length-3] + "..."
            logger.info(f"Cache hit: '{question[:40]}...' -> '{cached[:40]}...'")
            self.session_answers.append({"question": question, "answer": cached, "source": "cache"})
            self._log_to_kb(question, cached, "cache", field_type=field_type)
            return cached

        # Pre-compute generic fallback (used only if AI fails or is unavailable)
        generic_answer, generic_confidence = self._generate_generic_answer_with_confidence(question, field_type, max_length)

        # Try AI for a better answer
        if not GENAI_AVAILABLE or not self._ai_available or not self.api_key or not isinstance(self.api_key, str):
            logger.warning(f"AI unavailable — trying generic fallback: '{question[:50]}...'")
            if generic_confidence >= 85:
                source = "config_fallback"
                logger.info(f"Config fallback (confidence={generic_confidence}): '{question[:50]}...' -> '{generic_answer[:50]}...'")
                self.session_answers.append({"question": question, "answer": generic_answer, "source": source, "confidence": generic_confidence})
                self._log_to_kb(question, generic_answer, source, field_type=field_type)
                self._answer_cache[cache_key] = self._to_template(generic_answer)
                self._save_answer_cache()
                self._auto_learn_to_template_bank(question, generic_answer, field_type, "config_fallback")
                return generic_answer
            self.session_answers.append({"question": question, "answer": "", "source": "unsolved", "confidence": 0})
            self._log_to_kb(question, f"[UNSOLVED - AI unavailable] generic_suggestion={generic_answer}", "unsolved", field_type=field_type)
            return ""

        for attempt in range(self._retry_count):
            try:
                model = self._get_model()

                system = self.SYSTEM_PROMPT.format(
                    profile=self.profile_str,
                    company=self.job_context.get("company", "the company"),
                    role=self.job_context.get("role", "this role"),
                )

                user_prompt = f"Question: {question}\n"

                if options:
                    user_prompt += f"Available options: {', '.join(options)}\n"
                    user_prompt += "Select the most appropriate option or answer.\n"

                if max_length:
                    user_prompt += f"Keep your answer under {max_length} characters.\n"

                if field_type == "textarea":
                    user_prompt += "This is a long-form answer field. Provide a detailed response.\n"
                elif field_type in ("select", "radio"):
                    user_prompt += "Choose one option and respond with just that option.\n"
                elif field_type == "checkbox":
                    user_prompt += "This is a yes/no question. Start with Yes or No.\n"

                full_prompt = f"{system}\n\n{user_prompt}"

                # Use asyncio.wait_for with timeout to prevent long hangs
                try:
                    answer = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: model.generate_content(
                                full_prompt,
                                generation_config=genai.types.GenerationConfig(
                                    max_output_tokens=500,
                                    temperature=0.7,
                                )
                            )
                        ),
                        timeout=self._ai_timeout
                    )
                    answer = answer.text.strip()
                except asyncio.TimeoutError:
                    logger.warning(f"AI call timed out after {self._ai_timeout}s for: '{question[:50]}...'")
                    # Don't retry on timeout - fall through to generic answer
                    break

                if max_length and len(answer) > max_length:
                    answer = answer[:max_length-3] + "..."

                # Cache the answer for future use
                self._answer_cache[cache_key] = self._to_template(answer)
                self._save_answer_cache()
                self._track_ai_call(is_backup=self._using_backup, input_len=len(full_prompt), output_len=len(answer))

                source = "ai_backup" if self._using_backup else "ai"
                logger.info(f"{'Backup ' if self._using_backup else ''}AI answered: '{question[:50]}...' -> '{answer[:50]}...'")
                self.session_answers.append({"question": question, "answer": answer, "source": source})
                self._log_to_kb(question, answer, source, field_type=field_type)
                self._auto_learn_to_template_bank(question, answer, field_type, source)
                return answer

            except Exception as e:
                error_str = str(e).lower()
                if "429" in str(e) or "quota" in error_str or "rate" in error_str:
                    if attempt < self._retry_count - 1:
                        wait_time = self._retry_delay * (attempt + 1)
                        logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 2}/{self._retry_count}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        # Primary key exhausted — try backup
                        if not self._using_backup and self._switch_to_backup_key():
                            self._primary_exhausted = True
                            logger.info("Primary key exhausted, retrying with backup key...")
                            # Retry immediately with backup key
                            try:
                                model = self._get_model()
                                answer = await asyncio.wait_for(
                                    asyncio.get_running_loop().run_in_executor(
                                        None,
                                        lambda: model.generate_content(
                                            full_prompt,
                                            generation_config=genai.types.GenerationConfig(
                                                max_output_tokens=500,
                                                temperature=0.7,
                                            )
                                        )
                                    ),
                                    timeout=self._ai_timeout
                                )
                                answer = answer.text.strip()
                                if max_length and len(answer) > max_length:
                                    answer = answer[:max_length-3] + "..."
                                self._answer_cache[cache_key] = self._to_template(answer)
                                self._save_answer_cache()
                                self._track_ai_call(is_backup=True, input_len=len(full_prompt), output_len=len(answer))
                                logger.info(f"Backup AI answered: '{question[:50]}...' -> '{answer[:50]}...'")
                                self.session_answers.append({"question": question, "answer": answer, "source": "ai_backup"})
                                self._log_to_kb(question, answer, "ai_backup", field_type=field_type)
                                self._auto_learn_to_template_bank(question, answer, field_type, "ai_backup")
                                return answer
                            except Exception as backup_e:
                                logger.error(f"Backup AI also failed: {backup_e}")
                        self._ai_available = False
                        logger.warning("AI quota exceeded on all keys, falling back to generic answers")
                logger.error(f"AI answering failed: {e}")
                break

        # AI failed — try generic fallback (only if confidence >= 85)
        if generic_confidence >= 85:
            source = "config_fallback"
            logger.info(f"Config fallback after AI failure (confidence={generic_confidence}): '{question[:50]}...' -> '{generic_answer[:50]}...'")
            self.session_answers.append({"question": question, "answer": generic_answer, "source": source, "confidence": generic_confidence})
            self._log_to_kb(question, generic_answer, source, field_type=field_type)
            self._answer_cache[cache_key] = self._to_template(generic_answer)
            self._save_answer_cache()
            self._auto_learn_to_template_bank(question, generic_answer, field_type, "config_fallback")
            return generic_answer

        # Generic confidence is also low — leave field EMPTY for manual review
        # Don't fill garbage answers that hurt the application
        logger.warning(f"UNSOLVED QUESTION — leaving empty for manual: '{question[:80]}...'")
        self.session_answers.append({"question": question, "answer": "", "source": "unsolved", "confidence": 0})
        self._log_to_kb(question, f"[UNSOLVED - needs manual answer] generic_suggestion={generic_answer}", "unsolved", field_type=field_type)
        self.verifier.queue_for_review(
            question=question,
            proposed_answer=generic_answer,
            source="unsolved",
            confidence=QuestionVerifier.CONFIDENCE_GENERIC,
            field_type=field_type,
            options=options,
            company=self.job_context.get("company", ""),
        )
        # Return empty — handler will leave field empty, won't submit if required
        return ""

    def _get_option_matcher(self):
        """Get cached OptionMatcher instance (lazy-init)."""
        if self._option_matcher is None:
            from form.option_matcher import OptionMatcher
            self._option_matcher = OptionMatcher(self.config)
        return self._option_matcher

    def _match_option_from_config(self, question: str, options: List[str]) -> Optional[str]:
        """Try to match an option based on config values and question context."""
        return self._get_option_matcher().match_option(question, options)

    def _generate_generic_answer_with_confidence(self, question: str, field_type: str, max_length: Optional[int] = None) -> tuple:
        """Try generic answer and return (answer, confidence).

        Confidence levels:
          - 85: specific keyword match from config data (high quality, skip AI)
          - 60: broader keyword match or reasonable default (use if AI unavailable)
          - 0:  ultimate catch-all (should try AI first)
        """
        answer = self._generate_generic_answer(question, field_type, max_length, _return_confidence=True)
        if isinstance(answer, tuple):
            return answer
        # All early returns from _generate_generic_answer are specific matches → confidence 85
        return answer, 85

    def _generate_generic_answer(self, question: str, field_type: str, max_length: Optional[int] = None, _return_confidence: bool = False):
        """Generate a generic answer when AI and config both fail."""
        q = question.lower().strip()

        education = self.config.get("education", [{}])[0] if self.config.get("education") else {}
        skills = self.config.get("skills", {})
        demographics = self.config.get("demographics", {})
        personal = self.config.get("personal_info", {})
        company = self.job_context.get("company", "your company")

        # Confidence tracking: 85 = specific keyword match, 60 = broader match, 0 = ultimate catch-all
        _confidence = 85

        # ── CRITICAL: For SELECT/DROPDOWN fields, NEVER return long text ──
        # Dropdowns need short answers: Yes/No, a name, a number, etc.
        if field_type in ("select", "dropdown", "radio"):
            # Try yes/no detection for select fields
            yes_words = ["authorized", "eligible", "legally", "citizen", "willing", "able",
                         "agree", "consent", "acknowledge", "confirm", "accept", "comfortable",
                         "available", "can you", "do you", "are you", "have you read",
                         "open to", "18", "21", "over the age", "at least",
                         "perform essential", "reasonable accommodation",
                         "background check", "drug test", "relocat",
                         "ai usage policy", "usage policy"]
            no_words = ["employed with", "been employed", "previously employed",
                        "worked at", "worked for", "previously worked",
                        "ever been an", "ever been a",
                        "ever worked", "ever served",
                        "currently serving or have you",
                        "employment history",
                        "discharged", "asked to resign", "terminated from",
                        "fired from", "let go from", "involuntarily separated",
                        "non-compete", "restrictive", "criminal", "convicted",
                        "felony", "debarred", "ineligible", "disqualif", "excluded",
                        "suspended or debarred", "been suspended",
                        "require sponsor", "need sponsor", "require any",
                        "additional sponsor", "sponsorship for",
                        "refer you", "referred you", "someone refer",
                        "family member", "relative work", "know anyone",
                        "know of anyone",
                        "conflict of interest", "outside business", "party to any",
                        "restrict your right", "cuba, iran",
                        "close friend",
                        "certif", "license",
                        "cuba", "iran", "north korea", "syria", "crimea",
                        "national origin one of", "itar",
                        "tobacco", "smok", "nicotine", "vape",
                        "performed services", "performed work",
                        "private sector", "bid, proposal",
                        "citizen of another country",
                        "currently employ", "ever been employ",
                        "found liable", "found guilty",
                        "seta", "a&as"]
            if any(w in q for w in no_words):
                logger.info(f"Generic fallback (select→No): '{question[:40]}...' -> 'No'")
                return "No"
            if any(w in q for w in yes_words):
                logger.info(f"Generic fallback (select→Yes): '{question[:40]}...' -> 'Yes'")
                return "Yes"
            # For other dropdown questions, try short factual answers
            if any(x in q for x in ["state", "province"]):
                logger.info(f"Generic fallback (select→state): '{question[:40]}...'")
                return personal.get("state", "CA")
            if any(x in q for x in ["degree", "education", "level of study"]):
                logger.info(f"Generic fallback (select→degree): '{question[:40]}...'")
                return education.get("degree", "Bachelor's degree")
            if any(x in q for x in ["gender"]):
                logger.info(f"Generic fallback (select→gender): '{question[:40]}...'")
                return demographics.get("gender", "Prefer not to say")
            if any(x in q for x in ["ethnicity", "race"]):
                logger.info(f"Generic fallback (select→ethnicity): '{question[:40]}...'")
                return demographics.get("ethnicity", "Prefer not to say")
            if any(x in q for x in ["hear about", "how did you", "source", "find out"]):
                logger.info(f"Generic fallback (select→source): '{question[:40]}...'")
                return self.config.get("common_answers", {}).get("how_did_you_hear", "Online Job Board")
            if any(x in q for x in ["job title", "your title", "current title", "most recent title", "position title"]):
                work = self.config.get("work_experience", [{}])
                title = work[0].get("title", "Data Engineer") if work else "Data Engineer"
                logger.info(f"Generic fallback (select→job_title): '{question[:40]}...' -> '{title}'")
                return title
            if any(x in q for x in ["current employer", "most recent employer", "employer name", "company name"]):
                work = self.config.get("work_experience", [{}])
                employer = work[0].get("company", "Kruiz") if work else "Kruiz"
                logger.info(f"Generic fallback (select→employer): '{question[:40]}...' -> '{employer}'")
                return employer
            if any(x in q for x in ["reason for leaving", "reason for seeking", "why are you leaving", "why are you looking"]):
                logger.info(f"Generic fallback (select→reason): '{question[:40]}...'")
                return "Seeking internship opportunity to apply my skills"
            if any(x in q for x in ["referred by", "referral name", "who referred", "referral source"]):
                logger.info(f"Generic fallback (select→referral): '{question[:40]}...'")
                return "N/A"
            if any(x in q for x in ["reside in", "do you live in", "located in"]):
                logger.info(f"Generic fallback (select→reside): '{question[:40]}...'")
                return "No"
            if any(x in q for x in ["gpa", "grade point"]):
                logger.info(f"Generic fallback (select→gpa): '{question[:40]}...'")
                return str(education.get("gpa", "3.6"))
            if any(x in q for x in ["university", "school", "college", "institution"]):
                logger.info(f"Generic fallback (select→school): '{question[:40]}...'")
                return education.get("school", "San Jose State University")
            if any(x in q for x in ["veteran"]):
                logger.info(f"Generic fallback (select→veteran): '{question[:40]}...'")
                return "I am not a protected veteran"
            if any(x in q for x in ["disab"]):
                logger.info(f"Generic fallback (select→disability): '{question[:40]}...'")
                return "No, I don't have a disability"
            # Demographic checkbox labels — individual checkboxes from EEO forms
            # e.g. "Female", "Asian", "American Indian or Alaskan Native: Asian"
            # Match against config demographics to only check correct ones
            user_gender = demographics.get("gender", "").lower()  # "male"
            user_ethnicity = demographics.get("ethnicity", "").lower()  # "east asian"
            user_race = demographics.get("race", "").lower()  # "asian" (broader category)

            # Race/ethnicity labels that appear as checkboxes
            race_labels = [
                "american indian", "alaskan native", "native hawaiian",
                "pacific islander", "black or african", "white",
                "hispanic", "latino", "latina", "two or more races", "asian",
                "east asian", "south asian", "southeast asian",
            ]
            # Gender labels
            gender_labels = ["female", "male", "non-binary", "transgender",
                             "cisgender", "prefer not", "i decline", "i do not wish"]

            is_race_checkbox = any(x in q for x in race_labels)
            is_gender_checkbox = any(x in q for x in gender_labels)

            if is_race_checkbox:
                # Check if this specific race matches the user
                # Try both specific ethnicity ("east asian") and broad race ("asian")
                matches_user = False
                for user_val in [user_ethnicity, user_race]:
                    if user_val and user_val in q:
                        # Handle compound labels like "American Indian or Alaskan Native: Asian"
                        parts = q.split(":")
                        if len(parts) > 1:
                            sub = parts[-1].strip()
                            if user_val in sub:
                                matches_user = True
                                break
                            # Compound mismatch — don't match
                        else:
                            matches_user = True
                            break

                if matches_user:
                    logger.info(f"Generic fallback (race→Yes, matches '{user_ethnicity or user_race}'): '{question[:40]}...'")
                    return "Yes"
                logger.info(f"Generic fallback (race→No): '{question[:40]}...'")
                return "No"

            if is_gender_checkbox:
                # "male" is a substring of "female" — use word boundary check
                if user_gender and re.search(r'\b' + re.escape(user_gender) + r'\b', q):
                    logger.info(f"Generic fallback (gender→Yes, matches '{user_gender}'): '{question[:40]}...'")
                    return "Yes"
                logger.info(f"Generic fallback (gender→No): '{question[:40]}...'")
                return "No"
            # If we still can't figure it out for a dropdown, return "Yes" as safe default
            # (better than a paragraph of text that won't match any option)
            logger.warning(f"Generic fallback (select→Yes default, confidence=60): '{question[:40]}...' -> 'Yes'")
            if _return_confidence:
                return "Yes", 60
            return "Yes"

        # URL fields - MUST be checked first before generic fallbacks
        # LinkedIn
        if any(x in q for x in ["linkedin"]):
            answer = personal.get("linkedin", "")
            if answer:
                logger.info(f"Generic fallback (linkedin): '{question[:40]}...' -> '{answer}'")
                return answer

        # GitHub
        if any(x in q for x in ["github"]):
            answer = personal.get("github", "")
            if answer:
                logger.info(f"Generic fallback (github): '{question[:40]}...' -> '{answer}'")
                return answer

        # Website / Portfolio / Other website
        if any(x in q for x in ["website", "portfolio", "personal site", "personal url", "other website"]):
            answer = personal.get("portfolio", "") or personal.get("website", "")
            if answer:
                logger.info(f"Generic fallback (website): '{question[:40]}...' -> '{answer}'")
                return answer
            return ""  # Return empty rather than garbage for URL fields

        # Twitter / X URL — return empty
        if any(x in q for x in ["twitter", "x url"]) or q.strip().rstrip("*") in ["x", "twitter"]:
            logger.info(f"Generic fallback (twitter/x): '{question[:40]}...' -> empty")
            return ""

        # Facebook — N/A (no Facebook profile to share)
        if q.strip().rstrip("*") in ["facebook", "facebook url", "facebook profile"] or "facebook" in q:
            logger.info(f"Generic fallback (facebook): '{question[:40]}...' -> N/A")
            return "N/A"

        # Instagram — N/A
        if q.strip().rstrip("*") in ["instagram", "instagram url"] or "instagram" in q:
            logger.info(f"Generic fallback (instagram): '{question[:40]}...' -> N/A")
            return "N/A"

        # TikTok / Snapchat / other social — N/A
        if any(x in q for x in ["tiktok", "snapchat", "social media", "other social"]):
            logger.info(f"Generic fallback (social): '{question[:40]}...' -> N/A")
            return "N/A"

        # Any URL-looking field — return empty rather than garbage text
        if "url" in q and not any(x in q for x in ["linkedin", "github"]):
            logger.info(f"Generic fallback (unknown url): '{question[:40]}...' -> empty")
            return ""

        # Full name
        if q in ["full name*", "full name", "name*", "name"]:
            answer = personal.get("full_name", f"{personal.get('first_name', '')} {personal.get('last_name', '')}").strip()
            if answer:
                logger.info(f"Generic fallback (full_name): '{question[:40]}...' -> '{answer}'")
                return answer

        # First name
        if q in ["first name*", "first name", "first name:*"]:
            answer = personal.get("first_name", "")
            if answer:
                logger.info(f"Generic fallback (first_name): '{question[:40]}...' -> '{answer}'")
                return answer

        # Last name
        if q in ["last name*", "last name", "last name:*"]:
            answer = personal.get("last_name", "")
            if answer:
                logger.info(f"Generic fallback (last_name): '{question[:40]}...' -> '{answer}'")
                return answer

        # Email
        if q in ["email*", "email", "email address*", "email address"]:
            answer = personal.get("email", "")
            if answer:
                logger.info(f"Generic fallback (email): '{question[:40]}...' -> '{answer}'")
                return answer

        # Phone
        if q in ["phone*", "phone", "phone number*", "phone number"]:
            answer = personal.get("phone", "")
            if answer:
                logger.info(f"Generic fallback (phone): '{question[:40]}...' -> '{answer}'")
                return answer

        # Address
        if q in ["address*", "address", "street address*", "street address"]:
            answer = personal.get("address", "")
            if answer:
                logger.info(f"Generic fallback (address): '{question[:40]}...' -> '{answer}'")
                return answer

        # Zip code
        if any(x in q for x in ["zip", "postal"]) and "code" in q or q in ["zip*", "zip"]:
            answer = personal.get("zip_code", "")
            if answer:
                logger.info(f"Generic fallback (zip): '{question[:40]}...' -> '{answer}'")
                return answer

        # Country
        if q in ["country*", "country"]:
            answer = personal.get("country", "United States")
            if answer:
                logger.info(f"Generic fallback (country): '{question[:40]}...' -> '{answer}'")
                return answer

        # Date available for work
        if any(x in q for x in ["date available", "available for work", "date.*can.*start"]):
            avail = self.config.get("availability", {})
            answer = avail.get("earliest_start_date") or "05/19/2026"
            logger.info(f"Generic fallback: '{question[:40]}...' -> '{answer}...'")
            return answer

        # Standalone "Date" or "Date*" — typically signature date on CC-305 forms
        if q.strip().rstrip("*") == "date":
            import datetime
            answer = datetime.date.today().strftime("%m/%d/%Y")
            logger.info(f"Generic fallback (today's date): '{question[:40]}...' -> '{answer}'")
            return answer

        # Standalone "Name" or "Name*" — typically signature on CC-305 / self-identify forms
        if q.strip().rstrip("*") == "name":
            full_name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
            if full_name:
                logger.info(f"Generic fallback (name): '{question[:40]}...' -> '{full_name}'")
                return full_name

        # Standalone "Employee ID" — not an employee
        if "employee id" in q or "employee number" in q:
            logger.info(f"Generic fallback (employee id): '{question[:40]}...' -> 'N/A'")
            return "N/A"

        # Current base salary / compensation
        if any(x in q for x in ["current base salary", "current salary", "hourly rate", "compensation in the prior"]):
            logger.info(f"Generic fallback (salary): '{question[:40]}...' -> 'N/A - student'")
            return "N/A - student seeking internship"

        # Days/times available to work
        if "days and times" in q and "available" in q:
            logger.info(f"Generic fallback (availability): '{question[:40]}...'")
            return "Available Monday through Friday, 8:00 AM to 6:00 PM"

        # Educational institution name, city, state
        if "educational institution" in q and ("name" in q or "city" in q or "state" in q):
            school = education.get("school", "San Jose State University")
            city = personal.get("city", "San Jose")
            state = personal.get("state", "CA")
            answer = f"{school}, {city}, {state}"
            logger.info(f"Generic fallback (school info): '{question[:40]}...' -> '{answer}'")
            return answer

        # Specific field answers first (before generic fallback)
        # City (standalone)
        if q in ["city*", "city", "city:*"]:
            answer = personal.get("city", "San Francisco")
        # State (standalone)
        elif q.strip() in ["state*", "state", "state:*"]:
            answer = personal.get("state", "CA")
        # Timezone
        elif any(x in q for x in ["time zone", "timezone", "time-zone"]):
            avail = self.config.get("availability", {})
            answer = avail.get("timezone", "America/Los_Angeles") or "Pacific Time (PT)"
        # Current year in school
        elif any(x in q for x in ["year in school", "current year", "academic year"]):
            grad = education.get("graduation_date", self._get_grad_date())
            import re as _re2
            from datetime import datetime as _dt2
            _ym = _re2.search(r'20\d{2}', str(grad))
            _gy = int(_ym.group()) if _ym else _dt2.now().year
            _yug = _gy - _dt2.now().year
            if _yug <= 0:
                answer = "Senior"
            elif _yug == 1:
                answer = "Junior"
            elif _yug == 2:
                answer = "Sophomore"
            else:
                answer = "Freshman"
        # Pay/salary comfortable
        elif any(x in q for x in ["comfortable with", "agree to"]) and any(x in q for x in ["pay", "salary", "compensation"]):
            answer = "Yes"
        # Work in sector/industry
        elif any(x in q for x in ["worked in", "experience in"]) and any(x in q for x in ["sector", "industry", "energy"]):
            answer = "No"
        # Recruiting/career events attendance
        elif any(x in q for x in ["attend", "visit"]) and any(x in q for x in ["recruiting", "career", "fair", "event"]):
            answer = "No"
        # Relatives/family employed
        elif any(x in q for x in ["relative", "family", "friend"]) and any(x in q for x in ["employ", "work"]):
            answer = "No"
        # Company name (standalone or current employer)
        elif q.strip() in ["company name*", "company name", "company name:*"] or "company name" in q:
            exp = self.config.get("experience", [{}])[0] if self.config.get("experience") else {}
            answer = exp.get("company", "N/A")
        # Current employer
        elif any(x in q for x in ["current", "most recent", "previous"]) and any(x in q for x in ["employer", "company"]):
            exp = self.config.get("experience", [{}])[0] if self.config.get("experience") else {}
            answer = exp.get("company", "N/A")
        # "If you responded Yes to above" - usually should be empty/N/A
        elif "if you responded" in q and "yes" in q:
            answer = "N/A"
        # California residents additional information
        elif "california" in q and any(x in q for x in ["additional", "information", "resident"]):
            answer = "I acknowledge I have read and understand the additional information provided for California residents."
        # Degree status / highest degree
        elif any(x in q for x in ["degree status", "highest degree"]):
            answer = education.get("degree", "Bachelor's degree")
        # Specify details from above answer
        elif "specify" in q and any(x in q for x in ["details", "above", "answer"]):
            answer = "N/A"
        # Criminal history / felony conviction
        elif any(x in q for x in ["convicted", "felony", "misdemeanor", "crime"]):
            answer = "No"
        # Background check consent
        elif any(x in q for x in ["consent", "willing", "agree"]) and "background" in q:
            answer = "Yes"
        # Outstanding offers/deadlines
        elif "outstanding" in q and any(x in q for x in ["offer", "deadline"]):
            answer = "No"
        # Pursuing advanced degree
        elif "pursuing" in q and any(x in q for x in ["master", "phd", "doctorate", "graduate"]):
            answer = "Not at this time"
        # Women's specific programs - check config gender
        elif "women" in q and any(x in q for x in ["program", "internship", "interest"]):
            gender = demographics.get("gender", "").lower()
            answer = "Yes" if "female" in gender or "woman" in gender else "No"
        # Visa / immigration status
        elif any(x in q for x in ["visa", "immigration"]) and any(x in q for x in ["status", "type", "current"]):
            work_auth = self.config.get("work_authorization", {})
            answer = work_auth.get("current_visa_status") or ("Citizen" if work_auth.get("us_citizen") else "N/A")
        # Work authorization - unrestricted right to work
        elif any(x in q for x in ["unrestricted", "authorized", "eligible", "legally"]) and any(x in q for x in ["right to work", "work in", "employ"]):
            answer = "Yes"
        # Work onsite/hybrid/in-person questions
        elif any(x in q for x in ["able", "willing", "available"]) and any(x in q for x in ["onsite", "on-site", "office", "hybrid", "in person", "in-person"]):
            answer = "Yes"
        # Start on specific date questions
        elif any(x in q for x in ["able", "willing"]) and any(x in q for x in ["start on", "start date", "begin on"]):
            answer = "Yes"
        # Work schedule/hours questions
        elif any(x in q for x in ["able", "willing", "available"]) and any(x in q for x in ["work", "commit"]) and any(x in q for x in ["days", "hours", "week", "through", "until"]):
            answer = "Yes"
        # GPA (with spaces like G. P. A.)
        elif "g" in q and ("p" in q or "a" in q) and any(x in q for x in ["gpa", "g.p.a", "grade", "cumulative"]):
            answer = str(education.get("gpa", "3.5"))
        # Major / Field of study
        elif any(x in q for x in ["major", "field of study", "area of study", "concentration", "discipline"]):
            answer = education.get("field_of_study", "Computer Science")
        # School / University
        elif any(x in q for x in ["college", "university", "school", "institution"]) and any(x in q for x in ["attend", "which", "what", "your"]):
            answer = education.get("school", "University")
        # Pronouns
        elif "pronoun" in q:
            answer = demographics.get("pronouns") or "They/Them"
        # Race/ethnicity
        elif any(x in q for x in ["race", "ethnic", "background"]):
            answer = demographics.get("ethnicity", "Prefer not to say")
        # Transgender identity (must be before gender check — "transgender" contains "gender")
        elif "transgender" in q:
            answer = "No"
        # Gender
        elif "gender" in q:
            answer = demographics.get("gender", "Prefer not to say")
        # Internship term
        elif "internship" in q and any(x in q for x in ["term", "session", "period", "quarter"]):
            answer = self._get_internship_term()
        # Privacy policy acknowledgement
        elif "acknowledge" in q and any(x in q for x in ["privacy", "policy", "agree", "accept"]):
            answer = "Yes"
        # Current employee question
        elif "current" in q and "employee" in q:
            answer = "No"
        # Have you completed an internship before
        elif any(x in q for x in ["complet", "done", "had", "previous"]) and "internship" in q:
            answer = "Yes"
        # Academic year / class standing (Junior/Senior/Masters)
        elif any(x in q for x in ["junior", "senior", "masters", "class standing", "year in school", "academic year"]):
            grad = education.get("graduation_date", self._get_grad_date())
            # Determine class standing dynamically from graduation year
            import re as _re
            from datetime import datetime as _dt
            grad_year_match = _re.search(r'20\d{2}', str(grad))
            grad_year = int(grad_year_match.group()) if grad_year_match else _dt.now().year
            years_until_grad = grad_year - _dt.now().year
            if years_until_grad <= 0:
                answer = "Senior"
            elif years_until_grad == 1:
                answer = "Junior"
            elif years_until_grad == 2:
                answer = "Sophomore"
            else:
                answer = "Freshman"
        # Location/city office questions (city names in all caps like ATLANTA, NEW YORK)
        elif q.strip().isupper() or (len(q) < 30 and any(city in q.upper() for city in ["ATLANTA", "AUSTIN", "NEW YORK", "SAN FRANCISCO", "SEATTLE", "CHICAGO", "BOSTON", "DENVER", "LOS ANGELES"])):
            answer = "Yes"
        # Hybrid work arrangement
        elif "hybrid" in q and any(x in q for x in ["work", "arrangement", "schedule"]):
            answer = "Yes"
        # Open to / comfortable with location/arrangement
        elif any(x in q for x in ["open to", "comfortable"]) and any(x in q for x in ["hybrid", "remote", "onsite", "location", "arrangement"]):
            answer = "Yes"
        # Select option that describes situation/status
        elif "select" in q and any(x in q for x in ["option", "describes", "best"]):
            # This is a generic "select option" question - try to find a reasonable default
            answer = "Yes"
        # Veteran
        elif "veteran" in q:
            answer = "I am not a protected veteran"
        # Disability
        elif "disab" in q:
            answer = "No, I don't have a disability"
        # How did you hear / referral source
        elif any(x in q for x in ["hear about", "find out", "learn about", "how did you"]):
            answer = self.config.get("common_answers", {}).get("how_did_you_hear", "LinkedIn")
        # Have you previously worked/applied
        elif any(x in q for x in ["previous", "before", "prior"]) and any(x in q for x in ["work", "employ", "appl"]):
            answer = "No"
        # Referred by employee
        elif "refer" in q and any(x in q for x in ["employee", "current", "someone"]):
            answer = "No"
        # GPA
        elif "gpa" in q or "grade point" in q:
            answer = str(education.get("gpa", "3.5"))
        # Graduation date
        elif any(x in q for x in ["graduat", "slated", "complet"]) and any(x in q for x in ["date", "when", "year"]):
            answer = education.get("graduation_date", self._get_grad_date())
        # Start date / availability
        elif any(x in q for x in ["start", "begin", "available", "availability"]):
            avail = self.config.get("availability", {})
            answer = avail.get("earliest_start_date") or avail.get("preferred_start_date") or "Immediately"
        # Why interested questions (medium confidence — AI would do better for these)
        elif any(x in q for x in ["why", "interest", "excite", "motivat"]):
            _confidence = 60
            answer = (f"I'm excited about this opportunity at {company} because it aligns with my "
                     f"background in {education.get('field_of_study', 'Computer Science')} and my passion for "
                     f"building impactful software. I'm eager to contribute and grow with the team.")
        elif any(x in q for x in ["strength", "skill", "good at"]):
            _confidence = 60
            answer = ("I'm a fast learner with strong problem-solving abilities. I have hands-on experience "
                     f"with {', '.join(skills.get('frameworks', ['various technologies'])[:3])} and I work well in team environments.")
        elif any(x in q for x in ["weakness", "improve", "challenge"]):
            _confidence = 60
            answer = ("I sometimes spend too much time perfecting details. I'm actively working on "
                     "balancing thoroughness with meeting deadlines efficiently.")
        elif any(x in q for x in ["project", "accomplish", "proud"]):
            _confidence = 60
            projects = self.config.get("projects", [])
            if projects:
                p = projects[0]
                answer = f"I'm proud of {p.get('name', 'a recent project')}: {p.get('description', 'a software application')}."
            else:
                answer = "I built a full-stack application that helped me strengthen my skills in both frontend and backend development."
        elif "experience" in q:
            _confidence = 60
            answer = f"I have {skills.get('years_of_coding_experience', '3')} years of coding experience and have worked with technologies like {', '.join(skills.get('frameworks', ['React', 'Python'])[:3])}."
        else:
            # Ultimate generic fallback — confidence 0, should try AI first
            _confidence = 0
            answer = (f"I'm a motivated {education.get('degree', 'Computer Science')} student at "
                     f"{education.get('school', 'university')} with experience in software development. "
                     f"I'm eager to contribute to {company} and continue growing as an engineer.")

        if max_length and len(answer) > max_length:
            answer = answer[:max_length-3] + "..."

        logger.info(f"Generic fallback (confidence={_confidence}): '{question[:40]}...' -> '{answer[:40]}...'")
        if _return_confidence:
            return answer, _confidence
        return answer

    async def generate_cover_letter(self, job_description: str = "", max_words: int = 300) -> str:
        """Generate a tailored cover letter."""
        try:
            model = self._get_model()

            prompt = f"""Generate a professional cover letter for:
Company: {self.job_context.get('company', 'the company')}
Role: {self.job_context.get('role', 'this position')}

Candidate Profile:
{self.profile_str}

Job Description:
{job_description[:1000] if job_description else 'Not provided'}

Requirements:
- Keep it under {max_words} words
- Be genuine and enthusiastic
- Highlight relevant skills
- Show knowledge of the company if possible
- End with a call to action
"""

            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=800,
                    temperature=0.7,
                )
            )

            return response.text.strip()

        except Exception as e:
            logger.error(f"Cover letter generation failed: {e}")
            return ""

    async def match_option(self, question: str, options: list, config_value: Any) -> Optional[str]:
        """Use AI to match a config value to the best option."""
        # Try config-based matching first
        matched = self._match_option_from_config(question, options)
        if matched:
            return matched

        try:
            model = self._get_model()

            prompt = f"""Given this question and the user's answer, select the best matching option.

Question: {question}
User's intended answer: {config_value}
Available options: {', '.join(options)}

Respond with ONLY the exact option text that best matches the user's answer. If no option matches, respond with "NONE".
"""

            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=100,
                    temperature=0,
                )
            )

            answer = response.text.strip()

            if answer.upper() == "NONE":
                return None

            for option in options:
                if option.lower() == answer.lower():
                    return option

            return None

        except Exception as e:
            logger.error(f"Option matching failed: {e}")
            return None

    async def diagnose_with_gemini(self, prompt: str, max_output_tokens: int = 1000) -> Optional[dict]:
        """Call Gemini for structured JSON diagnosis (used by supervisor).

        Args:
            prompt: The diagnosis prompt (should request JSON output)
            max_output_tokens: Max tokens for response

        Returns:
            Parsed JSON dict, or None on failure
        """
        if not GENAI_AVAILABLE:
            logger.warning("google-generativeai not installed — cannot diagnose")
            return None

        model = self._get_model()
        if not model:
            logger.warning("No Gemini model available for diagnosis")
            return None

        try:
            response = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            max_output_tokens=max_output_tokens,
                            temperature=0.2,
                            response_mime_type="application/json",
                        )
                    )
                ),
                timeout=30
            )
            text = response.text.strip()
            self._track_ai_call(
                is_backup=self._using_backup,
                input_len=len(prompt),
                output_len=len(text),
            )
            # Update supervisor call counter
            self._cost_data.setdefault("supervisor_calls", 0)
            self._cost_data["supervisor_calls"] += 1
            self._save_cost_tracker()

            # Clean up common Gemini JSON issues
            # Strip markdown code fences
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)

            # Try parsing as-is first
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                # Try to repair truncated JSON — find last complete object/array
                # Try closing open brackets
                for fix in [text + "]", text + "}]", text + '"}]', text + '"}']:
                    try:
                        result = json.loads(fix)
                        logger.debug("Repaired truncated JSON from Gemini")
                        break
                    except json.JSONDecodeError:
                        continue
                else:
                    # Last resort: extract any JSON array or object
                    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
                    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
                    if arr_match:
                        try:
                            result = json.loads(arr_match.group())
                        except json.JSONDecodeError:
                            logger.warning(f"Gemini returned unparseable JSON ({len(text)} chars): {text[:200]}")
                            return None
                    elif obj_match:
                        try:
                            result = json.loads(obj_match.group())
                        except json.JSONDecodeError:
                            logger.warning(f"Gemini returned unparseable JSON ({len(text)} chars): {text[:200]}")
                            return None
                    else:
                        logger.warning(f"Gemini returned no JSON structure ({len(text)} chars): {text[:200]}")
                        return None

            if isinstance(result, dict):
                logger.info(f"Gemini diagnosis: strategy={result.get('strategy', '?')}, confidence={result.get('confidence', '?')}")
            else:
                logger.info(f"Gemini returned {type(result).__name__} with {len(result)} items")
            return result

        except asyncio.TimeoutError:
            logger.warning("Gemini diagnosis timed out after 30s")
            return None
        except Exception as e:
            error_str = str(e).lower()
            if ("429" in str(e) or "quota" in error_str) and not self._using_backup:
                if self._switch_to_backup_key():
                    logger.info("Primary key exhausted during diagnosis, retrying with backup...")
                    return await self.diagnose_with_gemini(prompt, max_output_tokens)
            logger.error(f"Gemini diagnosis failed: {e}")
            return None


async def main():
    """Test AI answerer."""
    import yaml

    with open("config/master_config.yaml", "r") as f:
        config = yaml.safe_load(f)

    answerer = AIAnswerer()
    answerer.set_profile(config)
    answerer.set_job_context("Google", "Software Engineering Intern")

    # Test questions - config fallback should handle these
    questions = [
        ("Why are you interested in this role?", "textarea"),
        ("Are you authorized to work in the US?", "select"),
        ("Will you require sponsorship?", "select"),
        ("Are you willing to relocate?", "select"),
        ("What is your greatest strength?", "textarea"),
        ("How did you hear about us?", "text"),
        ("When can you start?", "text"),
    ]

    print("AI Answer Test (with config fallback):")
    print("=" * 60)

    for q, ftype in questions:
        print(f"\nQ: {q}")
        answer = await answerer.answer_question(q, field_type=ftype)
        print(f"A: {answer[:100]}..." if len(answer) > 100 else f"A: {answer}")
        print("-" * 40)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
