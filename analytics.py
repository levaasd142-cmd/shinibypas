"""USD management reports. Integer cents, fulfilled sales, Moscow calendar dates."""
from datetime import date, datetime, timedelta, timezone
import math
import re
import time
import uuid

try:
    from zoneinfo import ZoneInfo
    MSK=ZoneInfo('Europe/Moscow')
except Exception:  # Windows without the tzdata package: Moscow has been UTC+3 since 2014.
    MSK=timezone(timedelta(hours=3))
CATEGORIES={'advertising':'Реклама','server':'Сервер и сервисы','fees':'Комиссии','other':'Прочее',
            'refund':'Возврат','replacement':'Замена товара'}


def deposit_prefixes():
    """Wallet ledger references that are customer deposits (Stars top-ups and every crypto network)."""
    try:
        from crypto_networks import NETWORKS
        networks=sorted(NETWORKS)
    except Exception:
        networks=['bsc','ton','tron','aptos']
    return ['topup:',*(name+':' for name in networks)]


def parse_day(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError
        return date.fromisoformat(value)
    except (ValueError,TypeError):raise ValueError('Укажите дату в формате ГГГГ-ММ-ДД') from None


def date_range(period='month',start='',end='',now=None):
    today=datetime.fromtimestamp(now if now is not None else time.time(),MSK).date()
    if period=='day':first=last=today
    elif period=='yesterday':first=last=today-timedelta(days=1)
    elif period=='week':first,last=today-timedelta(days=6),today
    elif period=='month':first,last=today.replace(day=1),today
    elif period=='custom':first,last=parse_day(start),parse_day(end)
    else:raise ValueError('Неизвестный период отчёта')
    if first<date(1970,1,1) or last>today:raise ValueError('Выберите даты от 1970 года до сегодняшнего дня')
    if not 0<=(last-first).days<366:raise ValueError('Период должен составлять от 1 до 366 дней')
    timestamp=lambda day:datetime.combine(day,datetime.min.time(),MSK).timestamp()
    return first,last,timestamp(first),timestamp(last+timedelta(days=1))


class Analytics:
    def __init__(self,store):self.store=store

    @staticmethod
    def usd(db):
        if not db.execute("SELECT 1 FROM meta WHERE key='stars_per_usd'").fetchone():
            raise ValueError('Сначала включите доллары магазина в разделе «Оплата и валюта»')

    def set_order_cost(self,order_id,amount,reason,actor):
        self.store.validate_cost(amount)
        if amount is None or not reason.strip() or len(reason)>300:raise ValueError('Укажите общую себестоимость и причину исправления')
        with self.store.tx() as db:
            self.usd(db)
            row=db.execute('SELECT cost_total FROM orders WHERE id=? AND charge IS NOT NULL',(order_id,)).fetchone()
            if not row:raise ValueError('Оплаченный заказ не найден')
            db.execute('UPDATE orders SET cost_total=?,cost_locked=1 WHERE id=?',(amount,order_id))
            self.store.log(db,actor,'finance.order_cost',order_id,order_id=order_id,
                details={'before':row['cost_total'],'after':amount,'reason':reason.strip()})
        return {'ok':True}

    def add_entry(self,reference,kind,category,amount,note,occurred_date,order_id,actor):
        try:reference=str(uuid.UUID(reference))
        except (ValueError,TypeError,AttributeError):raise ValueError('Неверный идентификатор операции') from None
        if kind not in ('expense','refund','replacement'):raise ValueError('Неизвестный вид операции')
        if category not in CATEGORIES or (kind=='expense')!=(category not in ('refund','replacement')) or (kind!='expense' and kind!=category):
            raise ValueError('Некорректная категория операции')
        self.store.validate_cost(amount)
        if not amount:raise ValueError('Сумма должна быть больше нуля')
        note=note.strip()
        if not note or len(note)>300:raise ValueError('Укажите описание до 300 символов')
        day=parse_day(occurred_date)
        if not date(1970,1,1)<=day<=datetime.now(MSK).date():raise ValueError('Выберите дату расхода от 1970 года до сегодняшнего дня')
        occurred_at=datetime.combine(day,datetime.min.time(),MSK).timestamp()
        order_id=order_id or None
        with self.store.tx() as db:
            self.usd(db)
            existing=db.execute('SELECT * FROM finance_entries WHERE id=?',(reference,)).fetchone()
            values=(kind,category,amount,note,occurred_at,order_id)
            if existing:
                if tuple(existing[k] for k in ('kind','category','amount','note','occurred_at','order_id'))!=values:
                    raise ValueError('Идентификатор уже использован для другой операции')
                return {'ok':True,'id':reference}
            if kind in ('refund','replacement'):
                order=db.execute("SELECT * FROM orders WHERE id=? AND status='fulfilled' AND charge IS NOT NULL",(order_id,)).fetchone()
                if not order:raise ValueError('Укажите полный номер выполненного оплаченного заказа')
                if day<datetime.fromtimestamp(order['fulfilled_at'] or order['paid_at'] or order['created'],MSK).date():
                    raise ValueError('Дата операции не может быть раньше выполнения покупки')
                if kind=='refund':
                    refunded=db.execute("SELECT coalesce(sum(amount),0) FROM finance_entries WHERE kind='refund' AND order_id=? AND voided_at IS NULL",(order_id,)).fetchone()[0]
                    if amount+refunded>order['price']:raise ValueError('Сумма возвратов превышает стоимость заказа')
            elif order_id:raise ValueError('Для обычного расхода привязка к заказу не нужна')
            db.execute('INSERT INTO finance_entries(id,kind,category,amount,note,occurred_at,order_id,created_at,actor) VALUES(?,?,?,?,?,?,?,?,?)',
                (reference,*values,time.time(),actor))
            self.store.log(db,actor,'finance.entry',reference,order_id=order_id,details={'amount':amount,'reason':note,'method':kind})
        return {'ok':True,'id':reference}

    def void_entry(self,reference,reason,actor):
        if not reason.strip() or len(reason)>300:raise ValueError('Укажите причину отмены')
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM finance_entries WHERE id=?',(reference,)).fetchone()
            if not row:raise ValueError('Операция не найдена')
            if row['voided_at'] is None:
                db.execute('UPDATE finance_entries SET voided_at=?,void_reason=? WHERE id=?',(time.time(),reason.strip(),reference))
                self.store.log(db,actor,'finance.void',reference,order_id=row['order_id'],details={'amount':row['amount'],'reason':reason.strip()})
        return {'ok':True}

    def daily_sales(self,days=30,now=None):
        """Dashboard: paid orders per Moscow day for the last `days` days (today included) plus the
        same-length period before it for deltas. Amounts are in the shop's price units (Stars or
        cents). An order counts on its payment day; legacy rows without paid_at use created."""
        if type(days) is not int or not 1<=days<=366:raise ValueError('Некорректный период')
        today=datetime.fromtimestamp(now if now is not None else time.time(),MSK).date()
        midnight=lambda day:datetime.combine(day,datetime.min.time(),MSK).timestamp()
        first_day=today-timedelta(days=days-1);previous_day=first_day-timedelta(days=days)
        first,previous,end=midnight(first_day),midnight(previous_day),midnight(today+timedelta(days=1))
        with self.store.read() as db:
            # Hourly buckets (Moscow offsets are whole hours) keep the per-day split in SQL, as in report().
            # Each branch is a range on its own index, orders(paid_at) and orders(created); the unary
            # '+' keeps the planner off orders(paid_at) for the IS NULL test.
            hours=db.execute('''SELECT CAST(at/3600 AS INTEGER) bucket,sum(price) revenue,count(*) orders FROM (
                SELECT paid_at at,price FROM orders WHERE paid_at>=? AND paid_at<? AND charge IS NOT NULL
                UNION ALL SELECT created,price FROM orders WHERE +paid_at IS NULL AND created>=? AND created<? AND charge IS NOT NULL)
                GROUP BY bucket''',(previous,end,previous,end)).fetchall()
            customers=db.execute('''SELECT coalesce(sum(first_seen>=?),0),coalesce(sum(first_seen<?),0) FROM telegram_users
                WHERE first_seen>=? AND first_seen<?''',(first,first,previous,end)).fetchone()
        buckets={}
        for row in hours:
            day=datetime.fromtimestamp(row['bucket']*3600,MSK).date()
            bucket=buckets.setdefault(day,[0,0]);bucket[0]+=row['revenue'] or 0;bucket[1]+=row['orders']
        series=[]
        for index in range(days):
            day=first_day+timedelta(days=index);revenue,count=buckets.get(day,(0,0))
            series.append(dict(date=day.isoformat(),revenue=revenue,orders=count))
        before=[buckets.get(previous_day+timedelta(days=index),(0,0)) for index in range(days)]
        revenue,count=sum(d['revenue'] for d in series),sum(d['orders'] for d in series)
        prev_revenue,prev_count=sum(r for r,_ in before),sum(c for _,c in before)
        return dict(series=series,kpi=dict(
            revenue=dict(value=revenue,previous=prev_revenue),orders=dict(value=count,previous=prev_count),
            average=dict(value=revenue//count if count else 0,previous=prev_revenue//prev_count if prev_count else 0),
            customers=dict(value=customers[0],previous=customers[1])))

    def report(self,period='month',start='',end='',page=1,expense_page=1,full=False):
        first,last,after,before=date_range(period,start,end)
        sold_at='coalesce(fulfilled_at,paid_at,created)'
        scope=f"FROM orders WHERE status='fulfilled' AND charge IS NOT NULL AND {sold_at}>=? AND {sold_at}<?"
        aggregate='sum(price) revenue,count(*) orders,sum(quantity) units,sum(cost_total IS NULL) unknown,coalesce(sum(cost_total),0) cost,sum(fulfilled_at IS NULL) estimated,coalesce(sum(discount_cents),0) discounts'
        with self.store.tx() as db:
            self.usd(db)
            # Aggregation happens in SQL: hourly buckets (Moscow offsets are whole hours) and per product.
            hours=db.execute(f'SELECT CAST({sold_at}/3600 AS INTEGER) bucket,{aggregate} {scope} GROUP BY bucket',(after,before)).fetchall()
            # Bare 'title' comes from the row holding max(): the latest sale, as before.
            product_rows=db.execute(f'''SELECT product_id,title,max({sold_at}) latest,{aggregate} {scope}
                GROUP BY product_id ORDER BY latest DESC,product_id''',(after,before)).fetchall()
            sales_total=sum(row['orders'] for row in hours)
            pages=max(1,math.ceil(sales_total/25));page=max(1,min(page,pages))
            limit,offset=(-1,0) if full else (25,(page-1)*25)
            sales=[dict(row) for row in db.execute(f'''SELECT id,product_id,title,price,quantity,cost_total,discount_cents,
                {sold_at} sold_at,fulfilled_at IS NULL estimated_date {scope}
                ORDER BY sold_at DESC,id LIMIT ? OFFSET ?''',(after,before,limit,offset))]
            entries=[dict(row) for row in db.execute('''SELECT f.*,o.product_id,o.title FROM finance_entries f
                LEFT JOIN orders o ON o.id=f.order_id WHERE occurred_at>=? AND occurred_at<?
                ORDER BY occurred_at DESC,created_at DESC,id''',(after,before))]
            # Deposits and balance adjustments are not sales revenue.
            prefixes=deposit_prefixes()
            deposits=db.execute('SELECT coalesce(sum(delta),0) FROM wallet_ledger WHERE created>=? AND created<? AND delta>0 AND ('
                +' OR '.join(["substr(reference,1,?)=?"]*len(prefixes))+')',
                (after,before,*[value for prefix in prefixes for value in (len(prefix),prefix)])).fetchone()[0]
            # Promo codes credit balances (3.2): a marketing cost, not a sale and not a deposit.
            promo_credits=db.execute("SELECT coalesce(sum(delta),0) FROM wallet_ledger WHERE created>=? AND created<? AND delta>0 AND substr(reference,1,6)='promo:'",
                (after,before)).fetchone()[0]
        def empty():return dict(revenue=0,cost=0,expenses=0,refunds=0,replacements=0,orders=0,units=0,unknown_cost_orders=0,discounts=0)
        total=empty();days={};products={};categories={}
        for index in range((last-first).days+1):days[(first+timedelta(days=index)).isoformat()]=empty()
        def product(pid,title):
            return products.setdefault(pid,dict(product_id=pid,title=title,**empty()))
        def add(target,row):
            target['revenue']+=row['revenue'];target['orders']+=row['orders'];target['units']+=row['units']
            target['unknown_cost_orders']+=row['unknown'];target['cost']+=row['cost'];target['discounts']+=row['discounts']
        estimated=0
        for row in hours:
            day=datetime.fromtimestamp(row['bucket']*3600,MSK).date().isoformat()
            add(total,row);add(days[day],row);estimated+=row['estimated']
        for row in product_rows:add(product(row['product_id'],row['title']),row)
        for entry in entries:
            if entry['voided_at'] is not None:continue
            day=datetime.fromtimestamp(entry['occurred_at'],MSK).date().isoformat()
            key='refunds' if entry['kind']=='refund' else 'expenses'
            targets=[total,days[day]]
            if entry['product_id'] is not None:targets.append(product(entry['product_id'],entry['title']))
            for target in targets:
                target[key]+=entry['amount']
                if entry['kind']=='replacement':target['replacements']+=entry['amount']
            if entry['kind']!='refund':categories[entry['category']]=categories.get(entry['category'],0)+entry['amount']
        for target in [total,*days.values(),*products.values()]:
            target['net_revenue']=target['revenue']-target['refunds']
            target['profit']=None if target['unknown_cost_orders'] else target['net_revenue']-target['cost']-target['expenses']
            target['margin']=round(target['profit']*100/target['net_revenue'],2) if target['profit'] is not None and target['net_revenue']>0 else None
        total['average_check']=(total['revenue']+total['orders']//2)//total['orders'] if total['orders'] else 0
        total['wallet_deposits']=deposits
        total['promo_credits']=promo_credits
        total['estimated_dates']=estimated
        def paginate(rows,value):
            pages=max(1,math.ceil(len(rows)/25));value=max(1,min(value,pages))
            return dict(items=rows if full else rows[(value-1)*25:value*25],page=value,pages=pages,total=len(rows))
        ranked=list(products.values())
        return dict(start=first.isoformat(),end=last.isoformat(),timezone='МСК (UTC+3)',summary=total,
            days=[dict(date=key,**value) for key,value in days.items()],
            top_revenue=sorted(ranked,key=lambda p:p['net_revenue'],reverse=True)[:20],
            top_profit=sorted(ranked,key=lambda p:(p['profit'] is not None,p['profit'] or 0),reverse=True)[:20],
            categories=[dict(category=key,title=CATEGORIES[key],amount=value) for key,value in categories.items()],
            sales=dict(items=sales,page=page,pages=pages,total=sales_total),entries=paginate(entries,expense_page))
