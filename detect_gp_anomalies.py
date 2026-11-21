import mysql.connector
import json
import time
import datetime
import os
from collections import defaultdict
from decimal import Decimal

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (Decimal, datetime.date, datetime.datetime, datetime.timedelta)):
            if isinstance(obj, Decimal):
                return float(obj)
            return str(obj)
        return super(DecimalEncoder, self).default(obj)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_thai_time():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)

def run_detection():
    thai_now = get_thai_time()
    print(f"[{thai_now.strftime('%Y-%m-%d %H:%M:%S')} THA] Starting Multi-Day GP anomaly detection across all branches (1/11/2026 to present, EXCLUDING 901)...")
    
    db_host = os.environ.get('DB_HOST', '203.154.83.62')
    db_port = int(os.environ.get('DB_PORT', '13306'))
    db_user = os.environ.get('DB_USER', 'powerbi')
    db_password = os.environ.get('DB_PASSWORD', 'pBp8@24019!')
    db_name = os.environ.get('DB_NAME', 'MYPOS2018_CENTER')
    start_date = os.environ.get('START_DATE', '2026-11-01')

    conn = mysql.connector.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name
    )
    cur = conn.cursor(dictionary=True)
    
    # 1. Fetch branch master info
    cur.execute("SELECT code, name, dm_code, dm, rm_code, rm, prvn_name FROM `data-lake`.dim_branch")
    branches_map = {b['code']: b for b in cur.fetchall()}
    print(f"Loaded {len(branches_map)} branches from dim_branch.")

    # 2. Get latest date
    cur.execute("SELECT MAX(sodate) as max_date FROM bl_header")
    max_d = cur.fetchone()['max_date']
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    target_date = max_d.strftime('%Y-%m-%d') if max_d else today_str
    print(f"Target analysis latest date: {target_date} (Start date: {start_date}, Excl 901)")

    # 3. Daily aggregate summaries for all dates from start_date
    cur.execute("""
        SELECT 
            sodate,
            COUNT(DISTINCT sotowhs) as active_branches,
            COUNT(*) as total_bills,
            SUM(soamount) as gross_sales,
            SUM(sodisc) as total_discount,
            SUM(sodisc_coupon) as total_coupon,
            SUM(sodisc_score) as total_score_disc,
            SUM(sonetamt) as total_net_sales,
            SUM(sototcost) as total_cost,
            SUM(sonetamt - sototcost) as total_gp,
            SUM(CASE WHEN sonetamt < sototcost THEN 1 ELSE 0 END) as neg_bills_count,
            SUM(CASE WHEN sonetamt < sototcost THEN sototcost - sonetamt ELSE 0 END) as total_loss_amount,
            MIN(sotime) as min_time,
            MAX(sotime) as max_time
        FROM bl_header
        WHERE sodate >= %s AND sotowhs NOT IN ('901', '0901')
        GROUP BY sodate
        ORDER BY sodate DESC
    """, (start_date,))
    daily_sums = cur.fetchall()

    daily_summary_list = []
    daily_summary_by_date = {}
    for d in daily_sums:
        d_str = str(d['sodate'])[:10]
        net = float(d['total_net_sales'] or 0)
        gp = float(d['total_gp'] or 0)
        bills = int(d['total_bills'] or 0)
        neg = int(d['neg_bills_count'] or 0)
        loss = float(d['total_loss_amount'] or 0)
        rec = {
            'date': d_str,
            'active_branches': d['active_branches'],
            'total_bills': bills,
            'gross_sales': float(d['gross_sales'] or 0),
            'total_discount': float(d['total_discount'] or 0),
            'total_coupon': float(d['total_coupon'] or 0),
            'total_score_disc': float(d['total_score_disc'] or 0),
            'total_net_sales': net,
            'total_cost': float(d['total_cost'] or 0),
            'total_gp': gp,
            'gp_pct': round(gp / net * 100, 2) if net > 0 else 0,
            'neg_bills_count': neg,
            'neg_bills_pct': round(neg / bills * 100, 2) if bills > 0 else 0,
            'total_loss_amount': round(loss, 2),
            'min_time': str(d['min_time']),
            'max_time': str(d['max_time'])
        }
        daily_summary_list.append(rec)
        daily_summary_by_date[d_str] = rec

    print(f"Processed daily summaries for {len(daily_summary_list)} days.")

    # 4. Fetch all negative GP bills from start_date
    cur.execute("""
        SELECT 
            h.sono, h.sodate, h.sotime, h.sotowhs, h.uname, h.cstcode,
            h.soamount, h.sodisc, h.sodisc_bill, h.sodisc_coupon, h.sodisc_score,
            h.sonetamt, h.sototcost,
            ROUND(h.sonetamt - h.sototcost, 2) as gp,
            ROUND((h.sonetamt - h.sototcost) / NULLIF(h.sonetamt, 0) * 100, 2) as gp_pct,
            ROUND(h.sototcost - h.sonetamt, 2) as loss_amt,
            h.sostatus, h.somethod, h.soremark
        FROM bl_header h
        WHERE h.sodate >= %s
          AND h.sonetamt < h.sototcost
          AND h.sotowhs NOT IN ('901', '0901')
        ORDER BY h.sodate DESC, (h.sototcost - h.sonetamt) DESC, h.sotime DESC
    """, (start_date,))
    all_neg_bills = cur.fetchall()
    print(f"Found total {len(all_neg_bills)} negative GP bills across {len(daily_summary_list)} days.")

    # 5. Fetch details for negative bills (full details for target_date and top 60 per day for others)
    bills_by_date = defaultdict(list)
    sonos_for_details = []
    
    for b in all_neg_bills:
        d_str = str(b['sodate'])[:10]
        whs = b['sotowhs']
        br_info = branches_map.get(whs, {})
        b['branch_name'] = br_info.get('name', f"สาขา {whs}")
        b['dm'] = br_info.get('dm') or 'ไม่ระบุ DM'
        b['rm'] = br_info.get('rm') or 'ไม่ระบุ RM'
        b['province'] = br_info.get('prvn_name', '-')
        b['items'] = []
        
        if d_str == target_date or len(bills_by_date[d_str]) < 60:
            sonos_for_details.append(b['sono'])
            
        bills_by_date[d_str].append(b)

    details_by_sono = {}
    item_stats_by_date = defaultdict(lambda: defaultdict(lambda: {
        'iprod': '', 'name': '', 'total_qty': 0, 'bills_count': set(),
        'normal_cost': 0, 'normal_price': 0, 'total_line_cost': 0, 'total_line_amt': 0
    }))

    if sonos_for_details:
        chunk_size = 500
        for i in range(0, len(sonos_for_details), chunk_size):
            chunk = sonos_for_details[i:i+chunk_size]
            placeholder = ', '.join(['%s'] * len(chunk))
            cur.execute(f"""
                SELECT 
                    d.sono, d.soserlno, d.iprod, d.soqty, d.sopricunit, d.sopricdisc,
                    d.solineamt, d.socstunit, d.untcode, d.soretflag, d.retqty,
                    d.solinetype, d.bldstatus,
                    p.idesc
                FROM bl_detail d
                LEFT JOIN `data-lake`.dim_product p ON d.iprod = p.iprod COLLATE utf8mb3_general_ci
                WHERE d.sono IN ({placeholder})
                ORDER BY d.sono, d.soserlno
            """, chunk)
            for d in cur.fetchall():
                details_by_sono.setdefault(d['sono'], []).append(d)

    # 6. Fetch coupon pairs for all dates from start_date
    cur.execute("""
        SELECT 
            b2.sodate as b2_date_full,
            cc.couponno, cc.couponamt, cc.billissue, cc.billuse, cc.cstcode,
            b2.sodate as b2_date, b2.sotime as b2_time, b2.sotowhs as b2_whs,
            b2.soamount as b2_amount, b2.sodisc_coupon as b2_coupon, b2.sonetamt as b2_net,
            b2.sototcost as b2_cost, (b2.sonetamt - b2.sototcost) as b2_gp,
            ROUND(((b2.sonetamt - b2.sototcost) / b2.sonetamt * 100), 2) as b2_gp_pct,
            b1.sodate as b1_date, b1.sotime as b1_time, b1.sotowhs as b1_whs,
            b1.soamount as b1_amount, b1.sodisc_coupon as b1_coupon, b1.sonetamt as b1_net,
            b1.sototcost as b1_cost, (b1.sonetamt - b1.sototcost) as b1_gp,
            ROUND(((b1.sonetamt - b1.sototcost) / b1.sonetamt * 100), 2) as b1_gp_pct
        FROM customer_coupon cc
        JOIN bl_header b2 ON cc.billuse = b2.sono
        LEFT JOIN bl_header b1 ON cc.billissue = b1.sono
        WHERE b2.sodate >= %s 
          AND (b2.sonetamt - b2.sototcost) < 0
          AND b2.sotowhs NOT IN ('901', '0901')
        ORDER BY b2.sodate DESC, b2.sotime DESC
    """, (start_date,))
    all_coupon_rows = cur.fetchall()
    print(f"Loaded {len(all_coupon_rows)} coupon pair rows.")

    coupons_by_date = defaultdict(list)
    for r in all_coupon_rows:
        d_str = str(r['b2_date'])[:10]
        b1_net = Decimal(str(r['b1_net'] or 0))
        b1_cost = Decimal(str(r['b1_cost'] or 0))
        b1_gp = Decimal(str(r['b1_gp'] or 0))
        b2_net = Decimal(str(r['b2_net'] or 0))
        b2_cost = Decimal(str(r['b2_cost'] or 0))
        b2_gp = Decimal(str(r['b2_gp'] or 0))
        comb_net = b1_net + b2_net
        comb_cost = b1_cost + b2_cost
        comb_gp = comb_net - comb_cost
        comb_gp_pct = round(comb_gp / comb_net * 100, 2) if comb_net > 0 else 0

        time_diff_str = "-"
        is_split = False
        if r['b1_date'] and r['b2_date'] and str(r['b1_date'])[:10] == str(r['b2_date'])[:10]:
            try:
                t1 = datetime.datetime.strptime(str(r['b1_time']), "%H:%M:%S")
                t2 = datetime.datetime.strptime(str(r['b2_time']), "%H:%M:%S")
                diff_sec = (t2 - t1).total_seconds()
                if diff_sec >= 0:
                    mins = int(diff_sec // 60)
                    secs = int(diff_sec % 60)
                    time_diff_str = f"{mins} น. {secs} วิ"
                    if mins <= 10:
                        is_split = True
            except Exception:
                pass
        elif r['b1_date']:
            time_diff_str = f"คนละวัน ({str(r['b1_date'])[:10]})"

        br = branches_map.get(r['b2_whs'], {})
        branch_name = br.get('name', f"สาขา {r['b2_whs']}")
        rm = br.get('rm', '-')
        dm = br.get('dm', '-')

        coupons_by_date[d_str].append({
            'cstcode': r['cstcode'],
            'branch_code': r['b2_whs'],
            'branch_name': branch_name,
            'rm': rm,
            'dm': dm,
            'coupon_amt': float(r['couponamt']),
            'time_diff': time_diff_str,
            'is_split': is_split,
            'b1_sono': r['billissue'] or '-',
            'b1_time': str(r['b1_time']) if r['b1_time'] else '-',
            'b1_net': float(b1_net),
            'b1_cost': float(b1_cost),
            'b1_gp': float(b1_gp),
            'b1_gp_pct': float(r['b1_gp_pct'] or 0),
            'b2_sono': r['billuse'],
            'b2_time': str(r['b2_time']),
            'b2_net': float(b2_net),
            'b2_cost': float(b2_cost),
            'b2_gp': float(b2_gp),
            'b2_gp_pct': float(r['b2_gp_pct'] or 0),
            'comb_net': float(comb_net),
            'comb_cost': float(comb_cost),
            'comb_gp': float(comb_gp),
            'comb_gp_pct': float(comb_gp_pct),
            'is_net_profit': bool(comb_gp >= 0)
        })

    # 7. Assemble daily records for each date
    available_dates = [d['date'] for d in daily_summary_list]
    daily_records = {}

    for d_str in available_dates:
        cur_summary = daily_summary_by_date[d_str]
        cur_bills = bills_by_date[d_str]
        cur_coupons = coupons_by_date[d_str]

        # Attach items and accumulate item stats
        day_item_map = {}
        for b in cur_bills:
            items = details_by_sono.get(b['sono'], [])
            b['items'] = items
            for it in items:
                iprod = it['iprod']
                if iprod not in day_item_map:
                    day_item_map[iprod] = {
                        'iprod': iprod,
                        'name': it['idesc'] or iprod,
                        'total_qty': 0,
                        'bills_count': set(),
                        'normal_cost': float(it['socstunit'] or 0),
                        'normal_price': float(it['sopricunit'] or 0),
                        'total_line_cost': 0,
                        'total_line_amt': 0
                    }
                q = float(it['soqty'] or 0)
                c = float(it['socstunit'] or 0)
                amt = float(it['solineamt'] or 0)
                day_item_map[iprod]['total_qty'] += q
                day_item_map[iprod]['bills_count'].add(b['sono'])
                day_item_map[iprod]['total_line_cost'] += q * c
                day_item_map[iprod]['total_line_amt'] += amt

        # Format Top Items for this day
        top_items_list = []
        for iprod, item in day_item_map.items():
            top_items_list.append({
                'iprod': iprod,
                'name': item['name'],
                'total_qty': item['total_qty'],
                'bills_count': len(item['bills_count']),
                'normal_cost': item['normal_cost'],
                'normal_price': item['normal_price'],
                'total_line_cost': round(item['total_line_cost'], 2),
                'total_line_amt': round(item['total_line_amt'], 2)
            })
        top_items_list.sort(key=lambda x: x['bills_count'], reverse=True)

        # RM & DM aggregations for this day
        rm_stats = defaultdict(lambda: {
            'rm_name': '',
            'branches': set(),
            'neg_bills': 0,
            'total_loss': 0.0,
            'dms': defaultdict(lambda: {'dm_name': '', 'branches': set(), 'neg_bills': 0, 'loss': 0.0})
        })
        branch_loss_summary = {}
        day_total_loss = sum(float(b['loss_amt']) for b in cur_bills)

        for b in cur_bills:
            whs = b['sotowhs']
            rm = b['rm']
            dm = b['dm']
            loss = float(b['loss_amt'])

            if whs not in branch_loss_summary:
                branch_loss_summary[whs] = {
                    'branch_code': whs,
                    'branch_name': b['branch_name'],
                    'dm': dm,
                    'rm': rm,
                    'province': b['province'],
                    'neg_bills_count': 0,
                    'total_loss': 0.0,
                    'total_net_sales': 0.0,
                    'total_coupon': 0.0
                }
            branch_loss_summary[whs]['neg_bills_count'] += 1
            branch_loss_summary[whs]['total_loss'] += loss
            branch_loss_summary[whs]['total_net_sales'] += float(b['sonetamt'])
            branch_loss_summary[whs]['total_coupon'] += float(b['sodisc_coupon'] or 0)

            rm_stats[rm]['rm_name'] = rm
            rm_stats[rm]['branches'].add(whs)
            rm_stats[rm]['neg_bills'] += 1
            rm_stats[rm]['total_loss'] += loss

            rm_stats[rm]['dms'][dm]['dm_name'] = dm
            rm_stats[rm]['dms'][dm]['branches'].add(whs)
            rm_stats[rm]['dms'][dm]['neg_bills'] += 1
            rm_stats[rm]['dms'][dm]['loss'] += loss

        rm_summary_list = []
        for rm, s in sorted(rm_stats.items(), key=lambda x: x[1]['total_loss'], reverse=True):
            dm_list = []
            for dm, ds in sorted(s['dms'].items(), key=lambda x: x[1]['loss'], reverse=True):
                brs_in_dm = []
                for whs in ds['branches']:
                    b_info = branch_loss_summary.get(whs)
                    if b_info:
                        brs_in_dm.append({
                            'branch_code': whs,
                            'branch_name': b_info['branch_name'],
                            'province': b_info['province'],
                            'total_loss': round(b_info['total_loss'], 2),
                            'neg_bills_count': b_info['neg_bills_count']
                        })
                brs_in_dm.sort(key=lambda x: x['total_loss'], reverse=True)
                dm_list.append({
                    'dm_name': dm,
                    'total_loss': round(ds['loss'], 2),
                    'loss_pct_in_rm': round((ds['loss'] / s['total_loss'] * 100), 1) if s['total_loss'] > 0 else 0,
                    'neg_bills_count': ds['neg_bills'],
                    'branches_affected': len(ds['branches']),
                    'branches': brs_in_dm
                })
            pct = (s['total_loss'] / day_total_loss * 100) if day_total_loss > 0 else 0
            rm_summary_list.append({
                'rm_name': rm,
                'total_loss': round(s['total_loss'], 2),
                'loss_pct': round(pct, 1),
                'neg_bills_count': s['neg_bills'],
                'branches_affected': len(s['branches']),
                'dms': dm_list
            })

        top_branches = list(branch_loss_summary.values())
        top_branches.sort(key=lambda x: x['total_loss'], reverse=True)

        # Coupon pairs summary
        cp_profit = sum(1 for p in cur_coupons if p['is_net_profit'])
        cp_loss = sum(1 for p in cur_coupons if not p['is_net_profit'])
        cp_split = sum(1 for p in cur_coupons if p['is_split'])
        b1_net_tot = sum(p['b1_net'] for p in cur_coupons)
        b1_cost_tot = sum(p['b1_cost'] for p in cur_coupons)
        b1_gp_tot = sum(p['b1_gp'] for p in cur_coupons)
        b2_net_tot = sum(p['b2_net'] for p in cur_coupons)
        b2_cost_tot = sum(p['b2_cost'] for p in cur_coupons)
        b2_gp_tot = sum(p['b2_gp'] for p in cur_coupons)
        comb_net_tot = b1_net_tot + b2_net_tot
        comb_cost_tot = b1_cost_tot + b2_cost_tot
        comb_gp_tot = comb_net_tot - comb_cost_tot

        cp_summary = {
            'total_pairs': len(cur_coupons),
            'net_profit_count': cp_profit,
            'net_profit_pct': round(cp_profit / len(cur_coupons) * 100, 1) if cur_coupons else 0,
            'net_loss_count': cp_loss,
            'net_loss_pct': round(cp_loss / len(cur_coupons) * 100, 1) if cur_coupons else 0,
            'split_bill_count': cp_split,
            'split_bill_pct': round(cp_split / len(cur_coupons) * 100, 1) if cur_coupons else 0,
            'total_b1_net': float(b1_net_tot),
            'total_b1_cost': float(b1_cost_tot),
            'total_b1_gp': float(b1_gp_tot),
            'total_b2_net': float(b2_net_tot),
            'total_b2_cost': float(b2_cost_tot),
            'total_b2_gp': float(b2_gp_tot),
            'total_combined_net': float(comb_net_tot),
            'total_combined_cost': float(comb_cost_tot),
            'total_combined_gp': float(comb_gp_tot),
            'overall_comb_gp_pct': round(comb_gp_tot / comb_net_tot * 100, 2) if comb_net_tot > 0 else 0
        }

        daily_records[d_str] = {
            'date': d_str,
            'summary': {
                **cur_summary,
                'branches_affected_count': len(branch_loss_summary)
            },
            'rm_summary': rm_summary_list,
            'top_branches_affected': top_branches[:25],
            'top_loss_items': top_items_list[:20],
            'recent_negative_bills': cur_bills[:250],
            'coupon_pairs': cur_coupons[:300],
            'coupon_pair_summary': cp_summary
        }

    # 8. Query Campaign Marketing Summary (Tab 5)
    cur.execute("""
        SELECT 
            cc.coupontype,
            cc.couponamt,
            COUNT(*) as issued_cnt,
            SUM(cc.couponamt) as issued_amt,
            -- Bill 1 (Issued bills)
            COUNT(DISTINCT cc.billissue) as b1_bill_cnt,
            SUM(IFNULL(b1.sonetamt, 0)) as b1_net_sales,
            SUM(IFNULL(b1.sototcost, 0)) as b1_cost,
            SUM(IFNULL(b1.sonetamt - b1.sototcost, 0)) as b1_gp,
            -- Bill 2 (Used bills)
            SUM(CASE WHEN cc.billuse IS NOT NULL AND TRIM(cc.billuse) != '' THEN 1 ELSE 0 END) as used_cnt,
            SUM(CASE WHEN cc.billuse IS NOT NULL AND TRIM(cc.billuse) != '' THEN cc.couponamt ELSE 0 END) as used_amt,
            SUM(IFNULL(b2.sonetamt, 0)) as b2_net_sales,
            SUM(IFNULL(b2.sototcost, 0)) as b2_cost,
            SUM(IFNULL(b2.sonetamt - b2.sototcost, 0)) as b2_gp
        FROM customer_coupon cc
        LEFT JOIN bl_header b1 ON cc.billissue = b1.sono
        LEFT JOIN bl_header b2 ON cc.billuse = b2.sono
        WHERE (cc.begdate >= %s OR cc.created_at >= %s)
          AND (b1.sotowhs NOT IN ('901', '0901') OR b1.sotowhs IS NULL)
          AND (b2.sotowhs NOT IN ('901', '0901') OR b2.sotowhs IS NULL)
        GROUP BY cc.coupontype, cc.couponamt
        ORDER BY cc.couponamt DESC
    """, (start_date, start_date))
    funnel_rows = cur.fetchall()

    total_issued_count = 0
    total_issued_amount = Decimal('0.00')
    total_used_count = 0
    total_used_amount = Decimal('0.00')
    tot_funnel_b1_sales = Decimal('0.00')
    tot_funnel_b1_gp = Decimal('0.00')
    tot_funnel_b2_sales = Decimal('0.00')
    tot_funnel_b2_gp = Decimal('0.00')
    tot_funnel_sales = Decimal('0.00')
    tot_funnel_gp = Decimal('0.00')

    # Preliminary pass for totals
    for fr in funnel_rows:
        b1_s = Decimal(str(fr['b1_net_sales'] or 0))
        b2_s = Decimal(str(fr['b2_net_sales'] or 0))
        b1_g = Decimal(str(fr['b1_gp'] or 0))
        b2_g = Decimal(str(fr['b2_gp'] or 0))
        comb_s = b1_s + b2_s
        comb_g = b1_g + b2_g
        tot_funnel_b1_sales += b1_s
        tot_funnel_b1_gp += b1_g
        tot_funnel_b2_sales += b2_s
        tot_funnel_b2_gp += b2_g
        tot_funnel_sales += comb_s
        tot_funnel_gp += comb_g

    funnel_list = []
    for fr in funnel_rows:
        i_cnt = int(fr['issued_cnt'] or 0)
        i_amt = Decimal(str(fr['issued_amt'] or 0))
        u_cnt = int(fr['used_cnt'] or 0)
        u_amt = Decimal(str(fr['used_amt'] or 0))
        b1_s = Decimal(str(fr['b1_net_sales'] or 0))
        b1_g = Decimal(str(fr['b1_gp'] or 0))
        b2_s = Decimal(str(fr['b2_net_sales'] or 0))
        b2_g = Decimal(str(fr['b2_gp'] or 0))
        comb_s = b1_s + b2_s
        comb_g = b1_g + b2_g

        red_pct = round(u_cnt / i_cnt * 100, 2) if i_cnt > 0 else 0
        b1_gp_pct = round(b1_g / b1_s * 100, 2) if b1_s > 0 else 0
        b2_gp_pct = round(b2_g / b2_s * 100, 2) if b2_s > 0 else 0
        comb_margin = round(comb_g / comb_s * 100, 2) if comb_s > 0 else 0
        sales_share = round(comb_s / tot_funnel_sales * 100, 2) if tot_funnel_sales > 0 else 0

        total_issued_count += i_cnt
        total_issued_amount += i_amt
        total_used_count += u_cnt
        total_used_amount += u_amt

        funnel_list.append({
            'coupontype': fr['coupontype'],
            'couponamt': float(fr['couponamt']),
            'issued_cnt': i_cnt,
            'issued_amt': float(i_amt),
            'b1_bill_cnt': int(fr['b1_bill_cnt'] or 0),
            'b1_net_sales': float(b1_s),
            'b1_gp': float(b1_g),
            'b1_gp_pct': float(b1_gp_pct),
            'used_cnt': u_cnt,
            'used_amt': float(u_amt),
            'redemption_pct': float(red_pct),
            'b2_net_sales': float(b2_s),
            'b2_gp': float(b2_g),
            'b2_gp_pct': float(b2_gp_pct),
            'total_sales': float(comb_s),
            'sales_share_pct': float(sales_share),
            'total_gp': float(comb_g),
            'combined_margin_pct': float(comb_margin)
        })

    cur.execute("""
        SELECT 
            CASE 
                WHEN b1.sodate IS NULL THEN 'NO_B1'
                WHEN b1.sodate < b2.sodate THEN 'DIFFERENT_DAY'
                WHEN b1.sodate = b2.sodate AND (TIME_TO_SEC(b2.sotime) - TIME_TO_SEC(b1.sotime)) BETWEEN 0 AND 600 THEN 'SPLIT_BILL_10M'
                WHEN b1.sodate = b2.sodate AND (TIME_TO_SEC(b2.sotime) - TIME_TO_SEC(b1.sotime)) > 600 THEN 'SAME_DAY_LATER'
                ELSE 'OTHER'
            END as timing_group,
            COUNT(*) as pair_count,
            COUNT(DISTINCT cc.cstcode) as unique_customers,
            SUM(cc.couponamt) as discount_spent,
            SUM(b2.sonetamt) as b2_sales,
            SUM(b2.sototcost) as b2_cost,
            SUM(b2.sonetamt - b2.sototcost) as b2_gp,
            SUM(IFNULL(b1.sonetamt, 0)) as b1_sales,
            SUM(IFNULL(b1.sototcost, 0)) as b1_cost,
            SUM(IFNULL(b1.sonetamt - b1.sototcost, 0)) as b1_gp,
            SUM((b2.sonetamt - b2.sototcost) + IFNULL(b1.sonetamt - b1.sototcost, 0)) as combined_net_gp
        FROM customer_coupon cc
        JOIN bl_header b2 ON cc.billuse = b2.sono
        LEFT JOIN bl_header b1 ON cc.billissue = b1.sono
        WHERE (cc.begdate >= %s OR b2.sodate >= %s)
          AND b2.sotowhs NOT IN ('901', '0901')
        GROUP BY timing_group
    """, (start_date, start_date))
    timing_rows = cur.fetchall()

    tot_camp_sales = Decimal('0.00')
    tot_camp_cost = Decimal('0.00')
    tot_camp_gp = Decimal('0.00')
    tot_camp_disc = Decimal('0.00')
    timing_list = []
    timing_label_map = {
        'SPLIT_BILL_10M': '⚡ แยกบิลซื้อซ้ำทันที (≤ 10 นาที)',
        'SAME_DAY_LATER': '🕒 ซื้อซ้ำในวันเดียวกัน (> 10 นาที)',
        'DIFFERENT_DAY': '📅 กลับมาซื้อซ้ำข้ามวัน (Customer Retention)',
        'NO_B1': '🎁 คูปองวันเกิด / คูปองแจกพิเศษ (ไม่ผูกบิลก่อนหน้า)',
        'OTHER': '📌 อื่นๆ'
    }
    for tr in timing_rows:
        b1_s = Decimal(str(tr['b1_sales'] or 0))
        b2_s = Decimal(str(tr['b2_sales'] or 0))
        b1_c = Decimal(str(tr['b1_cost'] or 0))
        b2_c = Decimal(str(tr['b2_cost'] or 0))
        comb_s = b1_s + b2_s
        comb_c = b1_c + b2_c
        comb_gp = comb_s - comb_c
        comb_gp_pct = round(comb_gp / comb_s * 100, 2) if comb_s > 0 else 0
        d_spent = Decimal(str(tr['discount_spent'] or 0))
        tot_camp_sales += comb_s
        tot_camp_cost += comb_c
        tot_camp_gp += comb_gp
        tot_camp_disc += d_spent
        timing_list.append({
            'timing_group': tr['timing_group'],
            'label': timing_label_map.get(tr['timing_group'], tr['timing_group']),
            'pair_count': int(tr['pair_count']),
            'unique_customers': int(tr['unique_customers']),
            'discount_spent': float(d_spent),
            'b1_sales': float(b1_s),
            'b2_sales': float(b2_s),
            'total_sales': float(comb_s),
            'total_cost': float(comb_c),
            'combined_net_gp': float(comb_gp),
            'combined_gp_pct': float(comb_gp_pct)
        })

    # Add sales share to timing list
    for t in timing_list:
        t['sales_share_pct'] = round(t['total_sales'] / float(tot_camp_sales) * 100, 2) if tot_camp_sales > 0 else 0

    cur.execute("""
        SELECT COUNT(DISTINCT cc.cstcode) as total_unique_members
        FROM customer_coupon cc
        JOIN bl_header b2 ON cc.billuse = b2.sono
        WHERE (cc.begdate >= %s OR b2.sodate >= %s)
          AND b2.sotowhs NOT IN ('901', '0901')
    """, (start_date, start_date))
    unique_members_cnt = cur.fetchone()['total_unique_members'] or 0

    camp_net_margin = round(tot_camp_gp / tot_camp_sales * 100, 2) if tot_camp_sales > 0 else 0
    sales_multiplier = round(tot_camp_sales / tot_camp_disc, 2) if tot_camp_disc > 0 else 0
    gp_multiplier = round(tot_camp_gp / tot_camp_disc, 2) if tot_camp_disc > 0 else 0
    overall_redemption_rate = round(total_used_count / total_issued_count * 100, 2) if total_issued_count > 0 else 0

    campaign_summary = {
        'start_date': start_date,
        'kpis': {
            'total_sales_generated': float(tot_funnel_sales),
            'total_b1_sales': float(tot_funnel_b1_sales),
            'total_b2_sales': float(tot_funnel_b2_sales),
            'total_net_gp': float(tot_funnel_gp),
            'net_gp_pct': round(float(tot_funnel_gp) / float(tot_funnel_sales) * 100, 2) if tot_funnel_sales > 0 else 0,
            'total_discount_spent': float(total_used_amount),
            'sales_roi_multiplier': round(float(tot_funnel_sales) / float(total_used_amount), 2) if total_used_amount > 0 else 0,
            'gp_roi_multiplier': round(float(tot_funnel_gp) / float(total_used_amount), 2) if total_used_amount > 0 else 0,
            'total_coupons_issued': total_issued_count,
            'total_issued_amount': float(total_issued_amount),
            'total_coupons_used': total_used_count,
            'total_used_amount': float(total_used_amount),
            'overall_redemption_rate': float(overall_redemption_rate),
            'unique_customers': int(unique_members_cnt)
        },
        'funnel': funnel_list,
        'funnel_totals': {
            'total_issued_cnt': total_issued_count,
            'total_issued_amt': float(total_issued_amount),
            'total_b1_sales': float(tot_funnel_b1_sales),
            'total_b1_gp': float(tot_funnel_b1_gp),
            'b1_margin_pct': round(float(tot_funnel_b1_gp) / float(tot_funnel_b1_sales) * 100, 2) if tot_funnel_b1_sales > 0 else 0,
            'total_used_cnt': total_used_count,
            'total_used_amt': float(total_used_amount),
            'redemption_pct': float(overall_redemption_rate),
            'total_b2_sales': float(tot_funnel_b2_sales),
            'total_b2_gp': float(tot_funnel_b2_gp),
            'total_sales': float(tot_funnel_sales),
            'total_gp': float(tot_funnel_gp),
            'combined_margin_pct': round(float(tot_funnel_gp) / float(tot_funnel_sales) * 100, 2) if tot_funnel_sales > 0 else 0
        },
        'timing_groups': timing_list,
        'timing_totals': {
            'pair_count': sum(t['pair_count'] for t in timing_list),
            'unique_customers': int(unique_members_cnt),
            'discount_spent': float(tot_camp_disc),
            'b1_sales': sum(t['b1_sales'] for t in timing_list),
            'b2_sales': sum(t['b2_sales'] for t in timing_list),
            'total_sales': float(tot_camp_sales),
            'total_gp': float(tot_camp_gp),
            'combined_gp_pct': round(float(tot_camp_gp) / float(tot_camp_sales) * 100, 2) if tot_camp_sales > 0 else 0
        }
    }

    conn.close()

    # Active/default record
    today_record = daily_records.get(target_date, list(daily_records.values())[0])

    payload = {
        'generated_at': thai_now.strftime('%Y-%m-%d %H:%M:%S'),
        'target_date': target_date,
        'available_dates': available_dates,
        'filter_note': 'ไม่รวมสาขา 901',
        'daily_summary_list': daily_summary_list,
        'daily_records': daily_records,
        # Default view references
        'summary': today_record['summary'],
        'rm_summary': today_record['rm_summary'],
        'top_branches_affected': today_record['top_branches_affected'],
        'top_loss_items': today_record['top_loss_items'],
        'recent_negative_bills': today_record['recent_negative_bills'],
        'negative_bills': today_record['recent_negative_bills'],
        'coupon_pairs': today_record['coupon_pairs'],
        'coupon_pair_summary': today_record['coupon_pair_summary'],
        'campaign_summary': campaign_summary
    }

    json_str = json.dumps(payload, ensure_ascii=False, cls=DecimalEncoder, indent=2)

    # 1. Save JSON file
    out_json = os.path.join(BASE_DIR, "gp_anomaly_data.json")
    with open(out_json, "w", encoding="utf-8") as f:
        f.write(json_str)
    print(f"Data saved to {out_json} ({len(json_str)/1024/1024:.2f} MB)")
    
    # 2. Save JS file for local access without CORS restrictions
    out_js = os.path.join(BASE_DIR, "gp_anomaly_data.js")
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = " + json_str + ";\n")
    print(f"Data saved to {out_js}")

    return payload

if __name__ == "__main__":
    res = run_detection()
    print("Multi-Day Detection Completed. Total Days:", len(res['available_dates']))
