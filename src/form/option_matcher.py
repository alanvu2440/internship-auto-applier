"""
Option Matcher Module

Extracts the option-matching logic from AIAnswerer into a shared, reusable class.
Given a question and a list of options, returns the best matching option based on
config values (work auth, demographics, education, etc.) and question context.
"""

import re
from datetime import datetime
from typing import Dict, Any, Optional, List


class OptionMatcher:
    """Matches dropdown/radio options to the best answer based on config values."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _get_grad_date(self) -> str:
        """Get graduation date from config, with sensible dynamic default."""
        education = self.config.get("education", [{}])
        if education and isinstance(education, list) and education[0]:
            grad = education[0].get("graduation_date", "")
            if grad:
                return str(grad)
        # Dynamic fallback: next May/June from current date
        now = datetime.now()
        year = now.year if now.month <= 6 else now.year + 1
        return f"May {year}"

    def _get_internship_term(self) -> str:
        """Get current internship term dynamically."""
        now = datetime.now()
        month = now.month
        year = now.year
        if month <= 4:
            return f"Summer {year}"
        elif month <= 8:
            return f"Fall {year}"
        else:
            return f"Summer {year + 1}"

    def match_option(self, question: str, options: List[str]) -> Optional[str]:
        """Try to match an option based on config values and question context."""
        q = question.lower()
        options_lower = [o.lower() for o in options]

        work_auth = self.config.get("work_authorization", {})
        screening = self.config.get("screening", {})
        availability = self.config.get("availability", {})
        demographics = self.config.get("demographics", {})

        # Authorization questions - including "unrestricted right to work"
        if any(x in q for x in ["authorized", "eligible", "legally", "right to work", "unrestricted"]):
            # Read config explicitly; missing keys → return None (don't guess)
            auth = work_auth.get("us_work_authorized")
            citizen = work_auth.get("us_citizen")
            if auth is True or citizen is True:
                # Explicitly authorized
                for i, opt in enumerate(options_lower):
                    if "yes" in opt:
                        return options[i]
                # If no "yes" option, look for "permanently authorized" type options
                for i, opt in enumerate(options_lower):
                    if "permanent" in opt or "authorized" in opt:
                        return options[i]
            elif auth is False and citizen is False:
                # Explicitly NOT authorized
                for i, opt in enumerate(options_lower):
                    if "no" in opt:
                        return options[i]
            else:
                # Missing config — return None, don't guess
                return None

        # Work permit type / authorization type (conditional follow-up)
        if any(x in q for x in ["work permit", "permit type", "authorization type", "work authorization"]):
            if work_auth.get("us_citizen", False) or work_auth.get("us_work_authorized", True):
                for i, opt in enumerate(options_lower):
                    if "permanent" in opt or "citizen" in opt or "authorized" in opt:
                        return options[i]
                # Fallback: first non-"select one" that isn't "temporary"
                for i, opt in enumerate(options_lower):
                    if "select" not in opt and "temporary" not in opt and opt.strip():
                        return options[i]

        # Sponsorship / immigration support questions
        if any(x in q for x in ["sponsor", "visa", "h1b", "h-1b", "immigration"]):
            need_sponsor = work_auth.get("require_sponsorship_now", False)
            if need_sponsor:
                for i, opt in enumerate(options_lower):
                    if "yes" in opt:
                        return options[i]
                # Fallback: first non-placeholder option
                for i, opt in enumerate(options_lower):
                    if opt.strip() and "select" not in opt:
                        return options[i]
            else:
                for i, opt in enumerate(options_lower):
                    if "no" in opt:
                        return options[i]
                # No "no"-containing option found — return None rather than guessing.
                # options[-1] is semantically wrong here (could be "Yes").
                return None

        # Relocation questions
        if "relocat" in q:
            if availability.get("willing_to_relocate", True):
                for i, opt in enumerate(options_lower):
                    if "yes" in opt:
                        return options[i]
            else:
                for i, opt in enumerate(options_lower):
                    if "no" in opt:
                        return options[i]

        # Age questions
        if any(x in q for x in ["18", "eighteen", "legal age", "21", "twenty"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Gender
        if "gender" in q:
            gender = demographics.get("gender", "Prefer not to say")
            gender_lower = gender.lower()
            # Exact match first (avoid "male" matching "female")
            for i, opt in enumerate(options_lower):
                if opt.strip() == gender_lower or opt.strip() == f"{gender_lower} ":
                    return options[i]
            # Word-boundary match
            for i, opt in enumerate(options_lower):
                if re.search(r'\b' + re.escape(gender_lower) + r'\b', opt):
                    return options[i]
            # Fallback to "prefer not to say"
            for i, opt in enumerate(options_lower):
                if "prefer not" in opt or "decline" in opt:
                    return options[i]

        # Ethnicity
        if any(x in q for x in ["ethnic", "race", "racial"]):
            ethnicity = demographics.get("ethnicity", "Prefer not to say")
            race = demographics.get("race", None)
            # Build candidate list — only include non-None, non-"prefer not to say" values
            candidates = []
            for val in [ethnicity, race]:
                if val and val.lower() not in ("prefer not to say", "prefer not to answer", "decline"):
                    candidates.append(val.lower())
            # Try each candidate against options
            for candidate in candidates:
                for i, opt in enumerate(options_lower):
                    if candidate in opt:
                        return options[i]
            # If no config value or all are "prefer not to say", fall through to decline
            for i, opt in enumerate(options_lower):
                if "prefer not" in opt or "decline" in opt:
                    return options[i]

        # Hispanic/Latino
        if "hispanic" in q or "latino" in q:
            ethnicity = demographics.get("ethnicity", "Prefer not to say")
            target = "yes" if "hispanic" in ethnicity.lower() or "latino" in ethnicity.lower() else "no"
            for i, opt in enumerate(options_lower):
                if target in opt:
                    return options[i]
            # Fallback to "no" or "prefer not to say"
            for i, opt in enumerate(options_lower):
                if "no" in opt or "prefer not" in opt or "decline" in opt:
                    return options[i]

        # Veteran status — detect by question text OR option text mentioning "veteran"
        options_joined = " ".join(options_lower)
        if "veteran" in q or ("veteran" in options_joined and ("government contractor" in q or "protected" in options_joined)):
            # Priority 1: "I am not a protected veteran" (exact non-veteran answer)
            for i, opt in enumerate(options_lower):
                if "i am not a protected veteran" in opt:
                    return options[i]
            # Priority 2: "I am not a veteran" or similar clear negation
            for i, opt in enumerate(options_lower):
                if opt.strip().startswith("i am not") and "veteran" in opt:
                    return options[i]
            # Priority 3: Any option with "not a veteran" that doesn't start with "I identify"
            for i, opt in enumerate(options_lower):
                if "not" in opt and "veteran" in opt and not opt.strip().startswith("i identify"):
                    return options[i]
            # Priority 4: Generic "no" or "not" option
            for i, opt in enumerate(options_lower):
                if "not" in opt or "no" in opt:
                    return options[i]

        # Polygraph / security clearance type — default to "None" option
        if "polygraph" in q or "clearance" in q and "type" in q:
            for i, opt in enumerate(options_lower):
                if opt.strip() == "none" or "no clearance" in opt or "never" in opt:
                    return options[i]

        # Disability — detect by question text OR option text mentioning "disability"
        # User confirmed: NO disability. Prioritize "No" answers over "prefer not to say"
        if "disab" in q or "disab" in options_joined:
            for i, opt in enumerate(options_lower):
                if "do not have" in opt or "no, i don" in opt or opt.strip() == "no":
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "do not want" in opt or "don't want" in opt or "don't wish" in opt or "i do not wish" in opt:
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "prefer not" in opt or "decline" in opt:
                    return options[i]

        # How did you hear / referral source
        if any(x in q for x in ["hear", "find out", "learn about", "source", "referral"]):
            source = self.config.get("common_answers", {}).get("how_did_you_hear", "LinkedIn")
            source_lower = source.lower()
            for i, opt in enumerate(options_lower):
                # Exact match
                if source_lower in opt:
                    return options[i]
                # LinkedIn variations
                if "linkedin" in source_lower and any(x in opt for x in ["linkedin", "social", "online", "job board", "internet"]):
                    return options[i]
            # Fallback to first reasonable option
            for i, opt in enumerate(options_lower):
                if any(x in opt for x in ["online", "job board", "internet", "social", "linkedin"]):
                    return options[i]

        # Office location / onsite preference questions
        # When options are office locations (NYC, SF, London, etc.), pick based on config location
        if any(x in q for x in ["onsite", "on-site", "in-person", "office", "days a week", "open to working"]):
            personal = self.config.get("personal_info", {})
            city = personal.get("city", "").lower()
            state = personal.get("state", "").lower()

            # Check if options look like office locations (not yes/no)
            has_yes_no = any("yes" in o for o in options_lower)
            has_city_options = any(len(o) < 30 and any(c in o for c in ["nyc", "sf", "london", "austin", "seattle", "chicago", "la", "boston", "denver", "remote"]) for o in options_lower)

            if has_city_options and not has_yes_no:
                # Map config city to common abbreviations
                city_map = {
                    "san francisco": ["sf", "san francisco"],
                    "new york": ["nyc", "new york"],
                    "los angeles": ["la", "los angeles"],
                    "chicago": ["chicago"],
                    "boston": ["boston"],
                    "seattle": ["seattle"],
                    "austin": ["austin"],
                    "denver": ["denver"],
                }
                for city_name, aliases in city_map.items():
                    if city and (city_name in city or city in city_name):
                        for i, opt in enumerate(options_lower):
                            if any(alias in opt for alias in aliases):
                                return options[i]
                        break

            # Fallback to yes for yes/no style questions
            if has_yes_no:
                for i, opt in enumerate(options_lower):
                    if "yes" in opt:
                        return options[i]

        # Work arrangement/onsite questions (yes/no style)
        if any(x in q for x in ["comfortable"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Current employee question
        if "current" in q and "employee" in q:
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Government/defense contractor negative questions — always "No"
        neg_keywords = [
            "performed services", "performed work",
            "suspended or debarred", "been suspended", "proposed for debarment",
            "private sector organization",
            "worked for u.s. government", "seta", "a&as",
            "found liable", "found guilty",
            "citizen of another country", "dual citizen",
        ]
        if any(kw in q for kw in neg_keywords):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Have you completed an internship before
        if any(x in q for x in ["complet", "done", "had", "previous"]) and "internship" in q:
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Academic year / class standing
        if any(x in q for x in ["junior", "senior", "masters", "class", "year", "standing"]):
            grad_date = self._get_grad_date()
            _ym3 = re.search(r'20\d{2}', str(grad_date))
            _gy3 = int(_ym3.group()) if _ym3 else datetime.now().year
            _diff = _gy3 - datetime.now().year
            target = "senior" if _diff <= 0 else "junior" if _diff == 1 else "sophomore" if _diff == 2 else "freshman"
            for i, opt in enumerate(options_lower):
                if target in opt:
                    return options[i]
            # Fallback to senior
            for i, opt in enumerate(options_lower):
                if "senior" in opt:
                    return options[i]

        # Location/city questions (all caps cities)
        if q.strip().isupper() or any(city in q.upper() for city in ["ATLANTA", "AUSTIN", "NEW YORK", "SAN FRANCISCO", "SEATTLE", "CHICAGO", "BOSTON"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # California residents / additional information acknowledgments
        if any(x in q for x in ["california", "ccpa", "additional information", "disclosure"]):
            for i, opt in enumerate(options_lower):
                # Look for acknowledgment options
                if any(x in opt for x in ["acknowledge", "i have read", "i understand", "yes", "agree"]):
                    return options[i]
            # If no clear acknowledgment option, try first option
            if options:
                return options[0]

        # General consent / agree / acknowledge questions
        if any(x in q for x in ["consent", "agree", "acknowledge", "do you accept"]):
            for i, opt in enumerate(options_lower):
                if any(x in opt for x in ["yes", "i agree", "i accept", "agree", "accept", "i consent"]):
                    return options[i]

        # Phone/SMS/text consent questions
        if any(x in q for x in ["sms", "text message", "phone number"]) and any(x in q for x in ["receive", "communication", "follow", "contact"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Hybrid/remote work arrangement
        if any(x in q for x in ["hybrid", "remote", "arrangement", "open to"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Degree type / education level
        if any(x in q for x in ["degree", "education level", "level of education"]):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                degree = education[0].get("degree", "Bachelor's")
                degree_lower = degree.lower()
                for i, opt in enumerate(options_lower):
                    if "bachelor" in degree_lower and "bachelor" in opt:
                        return options[i]
                    if "master" in degree_lower and "master" in opt:
                        return options[i]
                    if "phd" in degree_lower and ("phd" in opt or "doctor" in opt):
                        return options[i]

        # Background check consent
        if "background" in q and any(x in q for x in ["check", "screen", "investigation"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Drug test consent
        if "drug" in q and any(x in q for x in ["test", "screen"]):
            for i, opt in enumerate(options_lower):
                if "yes" in opt:
                    return options[i]

        # Internship term (Summer 2025, etc.)
        if "term" in q and "internship" in q:
            target_term = self._get_internship_term().lower()
            for i, opt in enumerate(options_lower):
                if target_term in opt:
                    return options[i]
            # Fallback to any summer option
            for i, opt in enumerate(options_lower):
                if "summer" in opt:
                    return options[i]

        # Graduation year dropdown
        if "graduation" in q and "year" in q:
            grad_date = self._get_grad_date()
            year_match = re.search(r'20\d{2}', str(grad_date))
            target_year = year_match.group() if year_match else str(
                datetime.now().year
            )
            for i, opt in enumerate(options_lower):
                if target_year in options[i]:
                    return options[i]

        # Previously applied / ever applied
        if ("previously" in q and "applied" in q) or ("ever" in q and "applied" in q) or "applied for work" in q:
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Referred by employee
        if "referred" in q or "referral" in q:
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Criminal history
        if any(x in q for x in ["convicted", "felony", "criminal", "misdemeanor"]):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Non-compete agreement
        if "non-compete" in q or "noncompete" in q:
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Tobacco / smoking
        if any(x in q for x in ["tobacco", "smok", "nicotine", "vape"]):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Relatives / family / friends at company
        if any(x in q for x in ["relative", "family", "close friend"]) and any(x in q for x in ["work", "employ", "parsons", "company", "organization", "subcontract", "supplier", "vendor", "client"]):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]
        # Broader: "are you a relative or close friend of any [Company]"
        if ("relative" in q or "close friend" in q) and "are you" in q:
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Previously employed / ever worked at company
        if (("previously" in q and ("employed" in q or "worked" in q))
            or ("ever" in q and ("worked" in q or "employed" in q or "employee" in q or "been" in q))
            or "worked for" in q):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Military service dropdown (including "Current Military Status" which has no "No" option)
        if any(x in q for x in ["military", "armed forces", "served"]) and any(x in q for x in ["ever", "current", "have you", "status"]):
            # Check if "No" option exists
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]
            # No "No" option (e.g. Current Military Status: Active/Terminal Leave/Retired/Other)
            # Return __SKIP__ to tell handler to leave this field alone
            return "__SKIP__"

        # Desired salary / annualized salary range — pick lowest for intern
        if any(x in q for x in ["desired annualized", "salary range", "desired salary", "salary expectation", "compensation range", "what is your desired salary"]):
            # Pick the lowest non-"Select One" range (intern-level)
            for i, opt in enumerate(options_lower):
                if "select" in opt or opt == "":
                    continue
                # Return first real option (usually lowest range)
                return options[i]

        # Desired hourly rate — pick the highest range
        if "hourly rate" in q or ("hourly" in q and "rate" in q) or "desired rate" in q:
            # Pick highest pay range available
            best_idx = -1
            best_val = 0
            for i, opt in enumerate(options_lower):
                nums = re.findall(r'\d+', opt)
                if nums:
                    max_num = max(int(n) for n in nums)
                    if max_num > best_val:
                        best_val = max_num
                        best_idx = i
            if best_idx >= 0:
                return options[best_idx]
            # Fallback: last option (usually highest)
            if options:
                return options[-1]

        # Hourly vs salary position
        if ("hourly" in q or "salary" in q) and ("desire" in q or "prefer" in q or "position" in q):
            for i, opt in enumerate(options_lower):
                if "hourly" in opt:
                    return options[i]
            # Fallback: pick "salary" if no hourly
            for i, opt in enumerate(options_lower):
                if "salary" in opt:
                    return options[i]

        # Type of employment desired
        if "type of employment" in q or "employment desired" in q:
            # Priority order: intern > both > full-time > part-time
            for i, opt in enumerate(options_lower):
                if "intern" in opt:
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "both" in opt or "open to" in opt:
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "full" in opt and "part" not in opt:
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "part" in opt:
                    return options[i]

        # Education level (highest level of education)
        if any(x in q for x in ["highest level", "level of education", "education.*completed", "education.*attained"]):
            for i, opt in enumerate(options_lower):
                if "some college" in opt or "currently attending" in opt or "college" in opt:
                    return options[i]
            for i, opt in enumerate(options_lower):
                if "bachelor" in opt:
                    return options[i]

        # Security clearance — "Do you currently hold a clearance?" with descriptive options
        if "clearance" in q and ("hold" in q or "have" in q or "currently" in q or "possess" in q):
            work_auth = self.config.get("work_authorization", {})
            if not work_auth.get("has_security_clearance", False):
                # Priority 1: "No, Never Held" or "No, I do not" (best answer for no clearance)
                for i, opt in enumerate(options_lower):
                    if "never" in opt and ("held" in opt or "clearance" in opt):
                        return options[i]
                # Priority 2: "do not have" WITHOUT "held" (never held before)
                for i, opt in enumerate(options_lower):
                    if "do not have" in opt and "held" not in opt:
                        return options[i]
                # Priority 3: "do not" or "not" + "clearance" (without "held in the past")
                for i, opt in enumerate(options_lower):
                    if ("do not" in opt or "not" in opt) and "clearance" in opt and "held" not in opt and "past" not in opt:
                        return options[i]
                # Priority 4: Exact "No"
                for i, opt in enumerate(options_lower):
                    if opt.strip() == "no":
                        return options[i]
                # Priority 5: "No" as first word
                for i, opt in enumerate(options_lower):
                    if opt.strip().startswith("no"):
                        return options[i]
                # Priority 6: Any option with "do not" (even if "held in past")
                for i, opt in enumerate(options_lower):
                    if "do not" in opt:
                        return options[i]

        # Security clearance level — if we don't have clearance, pick "None" or first option
        if "clearance" in q and any(x in q for x in ["highest", "level", "type", "previously obtained"]):
            work_auth = self.config.get("work_authorization", {})
            if not work_auth.get("has_security_clearance", False):
                for i, opt in enumerate(options_lower):
                    if "none" in opt or "n/a" in opt or "not applicable" in opt or "never" in opt:
                        return options[i]
                # If no "none" option and all are clearance levels, select first (required field)
                for i, opt in enumerate(options_lower):
                    if "select" not in opt and opt.strip():
                        return options[i]

        # Government employment history — "employment history with the U.S. Government"
        if ("government" in q and "employ" in q) or ("federal" in q and "employ" in q):
            for i, opt in enumerate(options_lower):
                if "never" in opt and ("employ" in opt or "government" in opt):
                    return options[i]
            # Fallback: option with "no" or "not"
            for i, opt in enumerate(options_lower):
                if opt.strip().startswith("i have never") or opt.strip().startswith("no") or opt.strip().startswith("not"):
                    return options[i]

        # Commitments / obligations to another employer
        if "commitment" in q and ("employer" in q or "organization" in q or "company" in q):
            for i, opt in enumerate(options_lower):
                if "no" in opt:
                    return options[i]

        # Name prefix (Mr./Mrs./Ms./Miss)
        if q.strip().rstrip("*. ") == "prefix" or ("prefix" in q and len(q) < 30):
            for i, opt in enumerate(options_lower):
                if "mr." in opt or opt.strip() == "mr":
                    return options[i]

        # Name suffix (Jr./Sr./II/III) — skip (optional)
        if q.strip().rstrip("*. ") == "suffix" or ("suffix" in q and len(q) < 30):
            # Don't select anything — suffix is optional
            return None

        # Notice period — pick shortest option (for internship applicants not currently employed)
        if "notice period" in q:
            for i, opt in enumerate(options_lower):
                if "15" in opt or "immediate" in opt or "none" in opt or "0" in opt:
                    return options[i]
            # Pick first non-placeholder option
            for i, opt in enumerate(options_lower):
                if opt.strip() != "select one" and opt.strip() != "" and opt.strip() != "select":
                    return options[i]

        # "Indicate the entity you work for" — not employed, pick "N/A" or first option
        if "entity you work for" in q or "indicate the entity" in q:
            for i, opt in enumerate(options_lower):
                if "n/a" in opt or "not applicable" in opt or "none" in opt or "other" in opt:
                    return options[i]

        # School/academic status questions (final year, second to last year, etc.)
        if "best describes your status" in q or ("which" in q and "status" in q and "school" in q):
            grad_date = self._get_grad_date()
            ym = re.search(r'20\d{2}', str(grad_date))
            gy = int(ym.group()) if ym else datetime.now().year
            diff = gy - datetime.now().year
            if diff <= 0:
                # Already graduated
                for i, opt in enumerate(options_lower):
                    if "earned" in opt or "past 12" in opt or "graduated" in opt:
                        return options[i]
            elif diff == 1:
                # Graduating next year = final year
                for i, opt in enumerate(options_lower):
                    if "final year" in opt or "last year" in opt:
                        return options[i]
            else:
                # 2+ years left = second to last or earlier
                for i, opt in enumerate(options_lower):
                    if "second to last" in opt or "earlier" in opt:
                        return options[i]
            # Fallback to "final year"
            for i, opt in enumerate(options_lower):
                if "final" in opt:
                    return options[i]

        # Graduation term/season (Spring, Summer, Fall, Winter)
        if ("term" in q or "when" in q or "season" in q) and ("graduat" in q or "finish" in q or "complet" in q):
            grad_date = self._get_grad_date()
            # Determine season from grad date
            grad_lower = str(grad_date).lower()
            if "may" in grad_lower or "june" in grad_lower or "apr" in grad_lower:
                target = "spring"
            elif "dec" in grad_lower or "nov" in grad_lower or "oct" in grad_lower:
                target = "fall"
            elif "aug" in grad_lower or "jul" in grad_lower or "sep" in grad_lower:
                target = "summer"
            else:
                target = "spring"  # Default to spring
            for i, opt in enumerate(options_lower):
                if target in opt:
                    return options[i]

        # School/university attending dropdown — match school name from config
        if any(x in q for x in ["university", "school", "college", "institution"]) and any(x in q for x in ["attend", "enrolled", "current", "name"]):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                school = education[0].get("school", "")
                if school:
                    school_lower = school.lower()
                    # Try exact match
                    for i, opt in enumerate(options_lower):
                        if school_lower == opt.strip():
                            return options[i]
                    # Try partial match (school name in option or option in school name)
                    for i, opt in enumerate(options_lower):
                        if school_lower in opt or opt.strip() in school_lower:
                            if opt.strip() and "select" not in opt:
                                return options[i]
                    # Try key words (e.g. "San Jose State" matches "San Jose State University")
                    school_words = [w for w in school_lower.split() if len(w) > 3]
                    for i, opt in enumerate(options_lower):
                        if sum(1 for w in school_words if w in opt) >= 2:
                            return options[i]
                    # If "Other" is an option, use it (school not in dropdown list)
                    for i, opt in enumerate(options_lower):
                        if opt.strip() == "other":
                            return options[i]

        # Future status questions — "What will your status be in [Month] [Year]?"
        # Derive from graduation date: if date is after graduation → graduated, else enrolled
        if "status" in q and ("will" in q or "what" in q) and re.search(r'20\d{2}', q):
            grad_date = self._get_grad_date()
            ym = re.search(r'20\d{2}', str(grad_date))
            grad_year = int(ym.group()) if ym else datetime.now().year
            grad_month_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)', str(grad_date).lower())
            grad_month_num = ["january","february","march","april","may","june","july","august","september","october","november","december"].index(grad_month_match.group()) + 1 if grad_month_match else 5

            # Extract target date from question
            q_year_match = re.search(r'(20\d{2})', q)
            q_month_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)', q)
            q_year = int(q_year_match.group()) if q_year_match else datetime.now().year
            q_month = ["january","february","march","april","may","june","july","august","september","october","november","december"].index(q_month_match.group()) + 1 if q_month_match else 9

            # Compare: is the question date after graduation?
            if (q_year > grad_year) or (q_year == grad_year and q_month > grad_month_num):
                # After graduation — look for graduated/alumni/completed
                for i, opt in enumerate(options_lower):
                    if any(x in opt for x in ["graduat", "alumni", "completed", "earned"]):
                        return options[i]
            else:
                # Before graduation — look for enrolled/student/attending
                for i, opt in enumerate(options_lower):
                    if any(x in opt for x in ["enrolled", "student", "attending", "current"]):
                        return options[i]
            # Fallback: first non-placeholder option
            for i, opt in enumerate(options_lower):
                if opt.strip() and "select" not in opt and "choose" not in opt:
                    return options[i]

        # Currently enrolled in degree program — yes/no or descriptive
        if any(x in q for x in ["enrolled", "currently enrolled"]) and any(x in q for x in ["bachelor", "master", "degree", "program"]):
            grad_date = self._get_grad_date()
            ym = re.search(r'20\d{2}', str(grad_date))
            grad_year = int(ym.group()) if ym else datetime.now().year
            is_still_enrolled = grad_year >= datetime.now().year
            for i, opt in enumerate(options_lower):
                if is_still_enrolled and "yes" in opt:
                    return options[i]
                elif not is_still_enrolled and "no" in opt:
                    return options[i]

        # "Describe your status" / enrollment status (not the school status one above)
        if ("describe" in q and "status" in q) or ("program" in q and "status" in q):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                degree = education[0].get("degree", "Bachelor")
                degree_lower = degree.lower()
                for i, opt in enumerate(options_lower):
                    if "bachelor" in opt and "bachelor" in degree_lower:
                        return options[i]
                    if "master" in opt and "master" in degree_lower:
                        return options[i]
                # Try "undergraduate" or "graduate"
                for i, opt in enumerate(options_lower):
                    if "bachelor" in degree_lower and ("undergrad" in opt or "pursuing" in opt):
                        return options[i]
                    if "master" in degree_lower and ("graduate" in opt or "pursuing" in opt):
                        return options[i]
                # Fallback: any option mentioning the degree type
                for i, opt in enumerate(options_lower):
                    if any(x in opt for x in ["bachelor", "bs", "b.s.", "undergrad"]):
                        return options[i]

        # Type of opportunity (Internship / New College Graduate / etc.)
        if "type of opportunity" in q or ("looking for" in q and "opportun" in q):
            for i, opt in enumerate(options_lower):
                if "intern" in opt:
                    return options[i]

        # Major / Field of study dropdown (not the typeahead — preset values)
        if any(x in q for x in ["major", "field of study"]):
            education = self.config.get("education", [{}])
            if isinstance(education, list) and education:
                fos = education[0].get("field_of_study", "Software Engineering").lower()
                # Try exact match first
                for i, opt in enumerate(options_lower):
                    if fos in opt:
                        return options[i]
                # Try individual words (e.g. "software" matches "Software Engineering")
                for word in fos.split():
                    if len(word) > 3:
                        for i, opt in enumerate(options_lower):
                            if word in opt:
                                return options[i]
                # Fallback: try related CS fields
                related = ["computer science", "engineering", "software", "information technology", "computer"]
                for r in related:
                    for i, opt in enumerate(options_lower):
                        if r in opt:
                            return options[i]

        # Questions where "No" is almost always the correct answer for an applicant
        no_patterns = [
            "currently work for",           # "Do you currently work for X company?"
            "serve as a director",          # "Do you serve as a director, officer..."
            "serve as an officer",
            "serve as a consultant",
            "government official",          # "Are you a government official?"
            "non-compete",                  # "Do you have a non-compete?"
            "relative.*work",               # "Do you have a relative who works here?"
            "family.*work",
            "family or close friend",       # "Do you have a Family or Close Friend..."
            "currently employed",           # "Are you currently employed by..."
            "previously worked for",        # Covered by radio too, but catch dropdown version
            "conflict of interest",         # "Do you have any COI?"
            "conflict with",                # "...conflict with [company]?"
            "outside business",             # "Do you have outside business activities?"
            "party to any employment",      # "Are you currently party to any employment..."
            "restrict your right",          # "...restricts your right to work/terminate..."
            "cuba, iran",                   # "Are you a citizen of Cuba, Iran, North Korea..."
        ]
        if any(re.search(p, q) if '.' in p or '*' in p else p in q for p in no_patterns):
            for i, opt in enumerate(options_lower):
                if opt.strip() == "no" or ("no" in opt and len(opt) < 20):
                    return options[i]

        # Option-based matching: when question text is generic but options reveal context
        # Work authorization options (e.g. "permanently authorized" / "temporary work permit")
        has_perm_auth = any("authorized" in opt and "permanent" in opt for opt in options_lower)
        has_temp_permit = any("temporary" in opt and ("permit" in opt or "work" in opt) for opt in options_lower)
        if has_perm_auth or has_temp_permit:
            if work_auth.get("us_citizen", False) or work_auth.get("us_work_authorized", True):
                for i, opt in enumerate(options_lower):
                    if "permanent" in opt:
                        return options[i]

        # Yes/No questions without specific patterns - generic catch-all (MUST BE LAST)
        yes_no_keywords = ["do you", "are you", "can you", "will you", "have you", "is your", "would you"]
        if any(k in q for k in yes_no_keywords):
            # Default to "Yes" for most yes/no questions (usually affirmative is what they want)
            for i, opt in enumerate(options_lower):
                if opt.strip() == "yes" or opt == "yes, i agree" or ("yes" in opt and len(opt) < 20):
                    return options[i]

        return None
