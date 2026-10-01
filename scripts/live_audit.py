"""Run on the hosting server: uses env keys and emits a report without secrets."""
from __future__ import annotations
import argparse
import asyncio
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')

from app.config import Settings
from app.services.live_audit import run_live_audit


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shop-id',type=int,default=1)
    parser.add_argument('--date',type=date.fromisoformat)
    parser.add_argument('--include-wb-stocks',action='store_true',help='Explicitly probe previously blocked WB stocks')
    parser.add_argument('--output',type=Path,help='New private JSON output file; omit to print sanitized JSON')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        settings=Settings.from_env()
        day=args.date or (datetime.now(ZoneInfo(settings.timezone)).date()-timedelta(days=1))
        report=asyncio.run(run_live_audit(settings,shop_id=args.shop_id,day=day,include_wb_stocks=args.include_wb_stocks))
        text=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
        if args.output:
            # Never overwrite the DB, env/config file, or an earlier audit.
            fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream: stream.write(text+'\n')
            print(json.dumps({'ok':report['ok'],'report_written':True,'optional_status':report.get('optional_status','skipped')}))
        else: print(text)
        return 0 if report['ok'] else 1
    except Exception:
        print(json.dumps({'ok':False,'error_kind':'audit_configuration_or_output_error'}))
        return 2


if __name__=='__main__': raise SystemExit(main())
