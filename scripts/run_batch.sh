#!/bin/bash
# Run batch apply — tabs stay open because process stays alive
# Press Ctrl+C to stop (browser tabs will close when process dies)
cd /Users/alan/internship-auto-applier
python src/main.py backfill --smart --with-simplify --max-open-tabs 20 --max 50
