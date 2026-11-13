"""Insights for the panel: weekday×hour heatmap, conversion funnel, top customers, monthly revenue
goal and purchase cohorts. Amounts are in the shop's price units (cents in USD mode, Stars before
it), Moscow calendar. Every query is index-driven or a single bounded scan (see loadtest timings in
CHANGELOG); results are cached for a few seconds per period because the page polls."""
import calendar
import copy
import threading
import time
from datetime import date, datetime, timedelta

from analytics import MSK, date_range

CACHE_SECONDS = 20
TOP_CUSTOMERS = 10
COHORT_MONTHS = 12
MSK_OFFSET = 3*3600  # Moscow has no DST since 2014; SQL month keys use the fixed offset.
WEEKDAYS = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')

# Paid orders counted on their payment moment; legacy rows without paid_at use created. Each branch
# is a range on its own index (orders(paid_at), orders(created)); '+' keeps the planner off paid_at.
PAID_IN_RANGE = '''SELECT paid_at at,price,user_id FROM orders WHERE paid_at>=? AND paid_at<? AND charge IS NOT NULL
  UNION ALL SELECT created,price,user_id FROM orders WHERE +paid_at IS NULL AND created>=? AND created<? AND charge IS NOT NULL'''


def month_start(day):
    return day.replace(day=1)


def add_months(day, months):
    index = day.year*12+day.month-1+months
    return date(index//12, index % 12+1, 1)


def midnight(day):
    return datetime.combine(day, datetime.min.time(), MSK).timestamp()


class Insights:
    def __init__(self, store):
        self.store = store
        self._cache = {}
        self._lock = threading.Lock()

    def _cached(self, key, build):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return copy.deepcopy(hit[1])
        value = build()
        with self._lock:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = (now+CACHE_SECONDS, value)
        return copy.deepcopy(value)

    def forget(self):
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------ report

    def report(self, period='month', start='', end='', now=None):
        first, last, after, before = date_range(period, start, end, now)
        key = (first.isoformat(), last.isoformat(), int((now or time.time())//CACHE_SECONDS))
        return self._cached(key, lambda: self._report(first, last, after, before, now))

    def _report(self, first, last, after, before, now):
        timings = {}

        def timed(name, function, *args):
            began = time.perf_counter()
            result = function(*args)
            timings[name] = round((time.perf_counter()-began)*1000, 1)
            return result
        with self.store.read() as db:
            heatmap = timed('heatmap', self.heatmap, db, after, before)
            funnel = timed('funnel', self.funnel, db, after, before)
            top = timed('top_customers', self.top_customers, db, after, before)
            goal = timed('goal', self.goal, db, now)
            # Cohorts and the goal do not depend on the period: shared by every period for a minute.
            cohorts = timed('cohorts', lambda: self._cached(('cohorts', int((now or time.time())//60)), lambda: self.cohorts(db, now)))
        return dict(start=first.isoformat(), end=last.isoformat(), timezone='МСК (UTC+3)',
                    currency='usd' if self.store.currency()['active'] else 'stars',
                    heatmap=heatmap, funnel=funnel, top_customers=top, goal=goal, cohorts=cohorts, timings=timings)

    # ------------------------------------------------------------------ parts

    @staticmethod
    def heatmap(db, after, before):
        """7×24 grid (Monday first) of paid orders and revenue by Moscow weekday and hour."""
        orders = [[0]*24 for _ in range(7)]
        revenue = [[0]*24 for _ in range(7)]
        rows = db.execute('SELECT CAST(at/3600 AS INTEGER) bucket,count(*) n,sum(price) amount FROM ('+PAID_IN_RANGE+
                          ') GROUP BY bucket', (after, before, after, before)).fetchall()
        for row in rows:
            moment = datetime.fromtimestamp(row['bucket']*3600, MSK)
            orders[moment.weekday()][moment.hour] += row['n']
            revenue[moment.weekday()][moment.hour] += row['amount'] or 0
        peak = max(((d, h) for d in range(7) for h in range(24)), key=lambda c: (orders[c[0]][c[1]], revenue[c[0]][c[1]]))
        return dict(weekdays=list(WEEKDAYS), orders=orders, revenue=revenue,
                    total_orders=sum(map(sum, orders)), total_revenue=sum(map(sum, revenue)),
                    peak=dict(weekday=peak[0], hour=peak[1], orders=orders[peak[0]][peak[1]]) if any(map(any, orders)) else None)

    @staticmethod
    def funnel(db, after, before):
        """New visitors of the period → created an order → paid → paid again (repeat buyers).
        Conversions are counted up to now. Catalog opens are not tracked by the bot."""
        row = db.execute('''SELECT count(*) visitors,coalesce(sum(o>0),0) ordered,coalesce(sum(p>0),0) paid,coalesce(sum(p>1),0) repeat FROM (
            SELECT EXISTS(SELECT 1 FROM orders o WHERE o.user_id=t.user_id AND o.created>=?) o,
                   (SELECT count(*) FROM (SELECT 1 FROM orders o WHERE o.user_id=t.user_id AND o.charge IS NOT NULL
                    AND o.created>=? LIMIT 2)) p
            FROM telegram_users t WHERE t.first_seen>=? AND t.first_seen<?)''', (after, after, after, before)).fetchone()
        steps = [('visitors', 'Новые посетители', row['visitors']), ('ordered', 'Создали заказ', row['ordered']),
                 ('paid', 'Оплатили', row['paid']), ('repeat', 'Купили повторно', row['repeat'])]
        top = steps[0][2]
        result, previous = [], None
        for key, label, value in steps:
            result.append(dict(key=key, label=label, value=value,
                               share=round(value*100/top, 1) if top else 0.0,
                               step=round(value*100/previous, 1) if previous else None))
            previous = value
        return dict(steps=result, tracked_catalog=False)

    @staticmethod
    def top_customers(db, after, before, limit=TOP_CUSTOMERS):
        rows = db.execute('SELECT user_id,sum(price) revenue,count(*) orders FROM ('+PAID_IN_RANGE+
                          ') GROUP BY user_id ORDER BY revenue DESC,user_id LIMIT ?', (after, before, after, before, limit)).fetchall()
        result = []
        for row in rows:
            # Covered by orders_paid_customer; an order is paid within its reservation, so 'created'
            # is the purchase moment to the minute.
            life = db.execute('''SELECT coalesce(sum(price),0) ltv,count(*) orders,max(created) last
                FROM orders WHERE user_id=? AND charge IS NOT NULL''', (row['user_id'],)).fetchone()
            person = db.execute('SELECT username,first_name,last_name FROM telegram_users WHERE user_id=?', (row['user_id'],)).fetchone()
            name = ' '.join(filter(None, [person['first_name'], person['last_name']])) if person else ''
            result.append(dict(user_id=row['user_id'], username=person['username'] if person else '', name=name,
                               revenue=row['revenue'], orders=row['orders'], ltv=life['ltv'], lifetime_orders=life['orders'],
                               last_purchase=life['last']))
        return result

    def goal(self, db, now=None):
        """Revenue of the current Moscow month against the owner's goal, with a linear forecast."""
        moment = now if now is not None else time.time()
        today = datetime.fromtimestamp(moment, MSK).date()
        first = month_start(today)
        start, end = midnight(first), midnight(add_months(first, 1))
        revenue = db.execute('SELECT coalesce(sum(price),0) FROM ('+PAID_IN_RANGE+')', (start, end, start, end)).fetchone()[0]
        row = db.execute("SELECT value FROM meta WHERE key='insights.revenue_goal'").fetchone()
        try:
            target = int(row[0]) if row else 0
        except ValueError:
            target = 0
        days_total = calendar.monthrange(first.year, first.month)[1]
        elapsed = max(1.0, (moment-start)/86400)  # less than a day of data would extrapolate wildly
        forecast = int(round(revenue*days_total/min(elapsed, days_total)))
        return dict(month=first.strftime('%Y-%m'), goal=target, revenue=revenue, forecast=forecast,
                    progress=round(revenue*100/target, 1) if target else None,
                    forecast_progress=round(forecast*100/target, 1) if target else None,
                    days_elapsed=round(min(elapsed, days_total), 2), days_total=days_total)

    def cohorts(self, db, now=None, months=COHORT_MONTHS):
        """Customers grouped by the Moscow month of their first paid order (last `months` months);
        each cell is the share of the cohort that paid again in month +k."""
        today = datetime.fromtimestamp(now if now is not None else time.time(), MSK).date()
        first_month = add_months(month_start(today), -(months-1))
        keys = [add_months(first_month, i).strftime('%Y-%m') for i in range(months)]
        index = {key: i for i, key in enumerate(keys)}
        # One grouped pass over the covering index of paid orders: (customer, month) pairs. The first
        # month is all-time, so a customer who first paid before the window is not counted as new.
        rows = db.execute('''SELECT user_id,strftime('%Y-%m',created+?,'unixepoch') m FROM orders
            WHERE charge IS NOT NULL GROUP BY user_id,m''', (MSK_OFFSET,)).fetchall()
        first, active = {}, {}
        for user_id, month in rows:
            if user_id not in first or month < first[user_id]:
                first[user_id] = month
            if month in index:
                active.setdefault(user_id, set()).add(index[month])
        size = [0]*months
        retained = [[0]*months for _ in range(months)]
        for user_id, month in first.items():
            start = index.get(month)
            if start is None:
                continue
            size[start] += 1
            for position in active.get(user_id, ()):
                if position >= start:
                    retained[start][position-start] += 1
        result = []
        for i, key in enumerate(keys):
            cells = []
            for offset in range(months-i):
                count = retained[i][offset]
                cells.append(dict(offset=offset, customers=count, share=round(count*100/size[i], 1) if size[i] else None))
            result.append(dict(month=key, customers=size[i], cells=cells))
        return dict(months=keys, rows=result)
