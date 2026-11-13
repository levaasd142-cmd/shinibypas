"""Portable XLSX report export using Office Open XML and the Python standard library."""
from datetime import date,datetime
from decimal import Decimal
from io import BytesIO
import re
import math
from xml.sax.saxutils import escape,quoteattr
from zipfile import ZipFile,ZIP_DEFLATED

from analytics import CATEGORIES,MSK

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
MONEY='"$"#,##0.00;[Red]-"$"#,##0.00'
XML_BAD=re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')


def column(index):
    result=''
    while index:
        index,remainder=divmod(index-1,26);result=chr(65+remainder)+result
    return result


def cell(reference,value,style=0):
    if value is None:return f'<c r="{reference}" s="{style}"/>'
    if isinstance(value,(datetime,date)):
        if not isinstance(value,datetime):value=datetime.combine(value,datetime.min.time())
        if value.tzinfo:value=value.astimezone(MSK).replace(tzinfo=None)
        serial=Decimal(str((value-datetime(1899,12,30)).total_seconds()))/Decimal(86400)
        return f'<c r="{reference}" s="{style or 4}"><v>{serial}</v></c>'
    if isinstance(value,(int,Decimal)):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    # Text is always inlineStr, including strings beginning with =, +, - or @.
    text=XML_BAD.sub('',str(value))
    if len(text.encode('utf-16-le'))//2>32767:
        raise ValueError('Текст в отчёте превышает размер ячейки Excel')
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def worksheet(archive,index,title,period,headers,rows,widths,styles,filters=True):
    last=column(len(headers))
    with archive.open(f'xl/worksheets/sheet{index}.xml','w') as output:
        def write(text):output.write(text.encode('utf-8'))
        write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{NS}">')
        write('<sheetViews><sheetView workbookViewId="0" showGridLines="0">')
        if filters:write('<pane ySplit="5" topLeftCell="A6" activePane="bottomLeft" state="frozen"/>')
        write('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="22"/><cols>')
        for col,width in enumerate(widths,1):write(f'<col min="{col}" max="{col}" width="{width}" customWidth="1"/>')
        write('</cols><sheetData>')
        write('<row r="2" ht="28" customHeight="1">'+cell('A2',title,2)+'</row>')
        write('<row r="3">'+cell('A3',period,6)+'</row>')
        write('<row r="5" ht="32" customHeight="1">'+''.join(cell(f'{column(i)}5',value,1) for i,value in enumerate(headers,1))+'</row>')
        last_row=5
        for last_row,row in enumerate(rows,6):
            if last_row>1048576:raise ValueError('Слишком много строк для Excel. Выберите более короткий период.')
            formats=styles(last_row-6) if callable(styles) else styles
            lines=max([1]+[sum(max(1,math.ceil(len(line)/max(1,widths[i]-2))) for line in str(value).split('\n')) for i,value in enumerate(row) if formats[i]==8])
            height=max(22,lines*15+6)
            write(f'<row r="{last_row}" ht="{height}" customHeight="1">'+''.join(cell(f'{column(i)}{last_row}',value,formats[i-1]) for i,value in enumerate(row,1))+'</row>')
        write('</sheetData>')
        if filters and last_row>5:write(f'<autoFilter ref="A5:{last}{last_row}"/>')
        write('<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>')


def analytics_xlsx(report):
    """Snapshot of the entire selected period; no pagination, formulas or external links."""
    if any(len(report[key]['items'])!=report[key]['total'] for key in ('sales','entries')):
        raise ValueError('Для Excel нужен полный отчёт за период')
    usd=lambda cents:Decimal(cents)/100 if cents is not None else 'Нет себестоимости'
    s=report['summary']
    period=f"{report['start']} — {report['end']} · МСК · суммы в USD"
    summary=[('Выручка',usd(s['revenue']),'Выполненные оплаченные заказы'),
        ('Начислено по промокодам',usd(s.get('promo_credits',0)),'На балансы покупателей; не выручка'),
        ('Себестоимость',usd(s['cost']),'Известная часть' if s['unknown_cost_orders'] else 'Все проданные товары'),
        ('Расходы',usd(s['expenses']),'Включая стоимость замен'),('Возвраты',usd(s['refunds']),'По дате операции'),
        ('Прибыль',usd(s['profit']),'Выручка − себестоимость − расходы − возвраты'),
        ('Маржинальность',Decimal(str(s['margin']))/100 if s['margin'] is not None else 'Не рассчитана','От выручки после возвратов'),
        ('Средний чек',usd(s['average_check']),'По выполненным заказам'),
        ('Заказы',s['orders'],'Выполнены за период'),('Продано единиц',s['units'],''),
        ('Заказы без себестоимости',s['unknown_cost_orders'],'При неполных данных прибыль не рассчитана'),
        ('Пополнения кошельков',usd(s['wallet_deposits']),'Не включены в выручку'),
        ('Приблизительные даты',s['estimated_dates'],'Для старых заказов без точной даты выполнения')]
    # Per-row formats on the summary are applied separately from detail-column formats.
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        names=['Итоги','По дням','Продажи','Расходы']
        archive.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,5))+'</Types>')
        archive.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml',f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView activeTab="0"/></bookViews><sheets>'+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,name in enumerate(names,1))+'</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,5))+'<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml',f'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{NS}">
          <numFmts count="3"><numFmt numFmtId="164" formatCode={quoteattr(MONEY)}/><numFmt numFmtId="165" formatCode="dd.mm.yyyy hh:mm"/><numFmt numFmtId="166" formatCode="dd.mm.yyyy"/></numFmts>
          <fonts count="4"><font><sz val="11"/><color rgb="FF172538"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font><font><b/><sz val="16"/><color rgb="FF172538"/><name val="Arial"/></font><font><i/><sz val="10"/><color rgb="FF61718A"/><name val="Arial"/></font></fonts>
          <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF172538"/><bgColor indexed="64"/></patternFill></fill></fills>
          <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
          <cellXfs count="9"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0"/><xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
        # Summary values are numeric; counts and the percentage get their own cell styles.
        formats=[3,3,3,3,3,3,7,3,0,0,0,3,0]
        worksheet(archive,1,'TheMarket — продажи и прибыль',period,['Показатель','Значение','Примечание'],summary,[38,25,76],lambda row:[0,formats[row],0],filters=False)
        days=((date.fromisoformat(d['date']),usd(d['revenue']),usd(d['cost']),usd(d['expenses']),usd(d['refunds']),usd(d['profit']),d['orders'],d['units'],d['unknown_cost_orders']) for d in report['days'])
        worksheet(archive,2,'Результат по дням',period,['Дата','Выручка, $','Себестоимость, $','Расходы, $','Возвраты, $','Прибыль, $','Заказы','Единицы','Без себестоимости'],days,[16,19,23,19,19,19,14,14,24],[5,3,3,3,3,3,0,0,0])
        sales=((datetime.fromtimestamp(r['sold_at'],MSK),r['id'],r['title'],r['quantity'],usd(r['price']),usd(r['cost_total']),'Приблизительная' if r['estimated_date'] else 'Точная') for r in report['sales']['items'])
        worksheet(archive,3,'Выполненные продажи',period,['Дата МСК','Заказ','Товар','Кол-во','Выручка, $','Себестоимость, $','Дата выполнения'],sales,[23,38,64,13,20,23,23],[4,0,8,0,3,3,0])
        entries=((datetime.fromtimestamp(r['occurred_at'],MSK),CATEGORIES[r['category']],usd(r['amount']),r['note'],r['order_id'] or '',r['title'] or '', 'Отменена' if r['voided_at'] is not None else 'Учтена',r['void_reason'] or '') for r in report['entries']['items'])
        worksheet(archive,4,'Расходы, возвраты и замены',period,['Дата МСК','Категория','Сумма, $','Описание','Заказ','Товар','Статус','Причина отмены'],entries,[16,25,19,68,38,54,16,50],[5,0,3,8,0,8,0,8])
    return output.getvalue()
