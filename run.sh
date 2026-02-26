#!/bin/bash
# Internship Auto-Applier - Run Script

cd "$(dirname "$0")"

# Activate virtual environment
source venv/bin/activate

# Check if config exists
if [ ! -f "config/master_config.yaml" ]; then
    echo "No config found. Using mock config for testing..."
    cp config/mock_config.yaml config/master_config.yaml
fi

# Run based on argument
case "$1" in
    test)
        echo "Running test mode (fills forms but doesn't submit)..."
        python3 src/test_apply.py
        ;;
    fetch)
        echo "Fetching jobs from SimplifyJobs..."
        python3 -c "
import asyncio
import sys
sys.path.insert(0, 'src')
from github_watcher import GitHubWatcher
from job_parser import JobParser, ATSType

async def main():
    watcher = GitHubWatcher()
    parser = JobParser()
    _, content = await watcher.check_for_changes()
    jobs = parser.parse_readme(content)
    await watcher.close()

    print(f'\\nTotal jobs: {len(jobs)}')

    ats_counts = {}
    for job in jobs:
        ats_counts[job.ats_type.value] = ats_counts.get(job.ats_type.value, 0) + 1

    print('\\nBy platform:')
    for ats, count in sorted(ats_counts.items(), key=lambda x: -x[1]):
        print(f'  {ats}: {count}')

    direct = [j for j in jobs if j.ats_type in (ATSType.GREENHOUSE, ATSType.LEVER)]
    print(f'\\nDirect-apply jobs (Greenhouse/Lever): {len(direct)}')

asyncio.run(main())
"
        ;;
    apply)
        echo "Starting auto-apply mode..."
        python3 src/main.py run
        ;;
    reset)
        echo "Resetting to mock config..."
        cp config/mock_config.yaml config/master_config.yaml
        rm -f data/jobs.db
        echo "Done! Edit config/master_config.yaml with your real info."
        ;;
    stats)
        python3 src/main.py stats
        ;;
    *)
        echo "Internship Auto-Applier"
        echo ""
        echo "Usage: ./run.sh [command]"
        echo ""
        echo "Commands:"
        echo "  fetch    - Fetch and count jobs from SimplifyJobs"
        echo "  test     - Test form filling (doesn't submit)"
        echo "  apply    - Start auto-applying to jobs"
        echo "  reset    - Reset config to mock data"
        echo "  stats    - Show application statistics"
        echo ""
        echo "Setup:"
        echo "  1. Edit config/master_config.yaml with your info"
        echo "  2. Add your resume to config/resume.pdf"
        echo "  3. Run: ./run.sh apply"
        ;;
esac
