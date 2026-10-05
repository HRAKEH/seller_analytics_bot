"""Export the exact ledger behind an accrual card, with readable Excel sheets."""
from pathlib import Path
from datetime import date
from openpyxl import Workbook

from app.reports.accrual_cards import accrual_totals, accrual_warnings, LABELS
from .spreadsheet_format import write_sheet, add_descriptions


def export_accrual_xlsx(ledger, path, *, marketplace, timezone, shop_name):
    totals=accrual_totals(ledger,marketplace)
    summary=[{'key':'Магазин','value':shop_name},
             {'key':'Период с' if marketplace=='ozon' else 'Даты сохранения с','value':ledger.start},
             {'key':'Период по' if marketplace=='ozon' else 'Даты сохранения по','value':ledger.end},
             {'key':'Загружено дней','value':len(ledger.loaded_dates)},
             {'key':'Операций' if marketplace=='ozon' else 'Финансовых отчётов','value':len(ledger.rows)}]
    if marketplace == 'wb':
        for start, end in sorted({(row['report_from'],row['report_to']) for row in ledger.rows}):
            summary.extend([{'key':'Период отчёта WB с','value':start or None},
                            {'key':'Период отчёта WB по','value':end or None}])
    summary.extend({'key':LABELS[key]+', ₽','value':value} for key,value in totals.items())
    summary.extend({'key':'Предупреждение','value':warning} for warning in accrual_warnings(ledger,marketplace))
    summary.append({'key':'Пояснение','value':'Это расчёт площадки. Поступление денег проверяется в банке.'})
    name='OzonOperations' if marketplace=='ozon' else 'WbReports'
    tables={'AccrualSummary':summary,name:list(ledger.rows)}
    wb=Workbook();wb.remove(wb.active)
    sheet=write_sheet(wb,'AccrualSummary',summary,timezone=timezone)
    # Summary values have mixed types; select formatting from their labels.
    for source,cells in zip(summary,sheet.iter_rows(min_row=2)):
        if source['key'].endswith(', ₽') and source['value'] is not None:
            cells[1].number_format='#,##0.00 "₽"'
        elif source['value'] and source['key'] in {'Период с','Период по','Даты сохранения с','Даты сохранения по',
                                                  'Период отчёта WB с','Период отчёта WB по'}:
            cells[1].value=date.fromisoformat(source['value']);cells[1].number_format='dd.mm.yyyy'
    write_sheet(wb,name,list(ledger.rows),timezone=timezone)
    add_descriptions(wb,tables)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);wb.save(path)
    return path
