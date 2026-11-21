import mysql.connector
import json
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

def run_detection():
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting GP anomaly detection across all branches (EXCLUDING 901)...")
    
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
    
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    cur.execute("SELECT MAX(sodate) as max_date FROM bl_header")
    max_d = cur.fetchone()['max_date']
    target_date = max_d.strftime('%Y-%m-%d') if max_d else today_str
        
    print(f"Target analysis date: {target_date} (Excluding Branch 901)")
    
    # 1. Overall stats for target date (excluding 901)
    cur.execute("""
        SELECT 
            COUNT(DISTINCT sotowhs) as active_branches,
            COUNT(*) as total_bills,
            SUM(soamount) as gross_sales,
            SUM(sodisc) as total_discount,
            SUM(sodisc_coupon) as total_coupon,
            SUM(sodisc_score) as total_score_disc,
            SUM(sonetamt) as total_net_sales,
            SUM(sototcost) as total_cost,
            SUM(sonetamt - sototcost) as total_gp,
            MIN(sotime) as min_time,
            MAX(sotime) as max_time
        FROM bl_header
        WHERE sodate = %s AND sotowhs NOT IN ('901', '0901')
    """, (target_date,))
    overall = cur.fetchone()
    
    # 2. Fetch all negative GP bills (sonetamt < sototcost, excluding 901)
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
        WHERE h.sodate = %s 
          AND h.sonetamt < h.sototcost
          AND h.sotowhs NOT IN ('901', '0901')
        ORDER BY (h.sototcost - h.sonetamt) DESC, h.sotime DESC
    """, (target_date,))
    neg_bills = cur.fetchall()
    
    print(f"Found {len(neg_bills)} negative GP bills (excluding branch 901).")
    
    # 3. Fetch branch master info
    cur.execute("SELECT code, name, dm_code, dm, rm_code, rm, prvn_name FROM `data-lake`.dim_branch")
    branch_rows = cur.fetchall()
    branches_map = {b['code']: b for b in branch_rows}
    
    # 4. Fetch details for all negative GP bills
    neg_sonos = [b['sono'] for b in neg_bills]
    details_by_sono = {}
    item_loss_summary = {}
    
    if neg_sonos:
        chunk_size = 500
        for i in range(0, len(neg_sonos), chunk_size):
            chunk = neg_sonos[i:i+chunk_size]
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
                
                iprod = d['iprod']
                desc = d['idesc'] or iprod
                qty = float(d['soqty'] or 0)
                cost = float(d['socstunit'] or 0)
                price = float(d['sopricunit'] or 0)
                lineamt = float(d['solineamt'] or 0)
                line_cost = qty * cost
                
                if iprod not in item_loss_summary:
                    item_loss_summary[iprod] = {
                        'iprod': iprod,
                        'name': desc,
                        'total_qty': 0,
                        'bills_count': set(),
                        'normal_cost': cost,
                        'normal_price': price,
                        'total_line_cost': 0,
                        'total_line_amt': 0
                    }
                item_loss_summary[iprod]['total_qty'] += qty
                item_loss_summary[iprod]['bills_count'].add(d['sono'])
                item_loss_summary[iprod]['total_line_cost'] += line_cost
                item_loss_summary[iprod]['total_line_amt'] += lineamt

    # Enrich negative bills with branch info and items
    branch_loss_summary = {}
    rm_stats = defaultdict(lambda: {
        'rm_name': '',
        'branches': set(),
        'neg_bills': 0,
        'total_loss': 0.0,
        'dms': defaultdict(lambda: {'dm_name': '', 'branches': set(), 'neg_bills': 0, 'loss': 0.0})
    })

    total_loss_sum = sum(float(b['loss_amt']) for b in neg_bills)

    for b in neg_bills:
        whs = b['sotowhs']
        br_info = branches_map.get(whs, {})
        b['branch_name'] = br_info.get('name', f"สาขา {whs}")
        rm = br_info.get('rm') or 'ไม่ระบุ RM'
        dm = br_info.get('dm') or 'ไม่ระบุ DM'
        b['dm'] = dm
        b['rm'] = rm
        b['province'] = br_info.get('prvn_name', '-')
        
        b['items'] = details_by_sono.get(b['sono'], [])
        loss = float(b['loss_amt'])
        
        # Branch aggregate
        if whs not in branch_loss_summary:
            branch_loss_summary[whs] = {
                'branch_code': whs,
                'branch_name': b['branch_name'],
                'dm': b['dm'],
                'rm': b['rm'],
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
        
        # RM & DM aggregate
        rm_stats[rm]['rm_name'] = rm
        rm_stats[rm]['branches'].add(whs)
        rm_stats[rm]['neg_bills'] += 1
        rm_stats[rm]['total_loss'] += loss
        
        rm_stats[rm]['dms'][dm]['dm_name'] = dm
        rm_stats[rm]['dms'][dm]['branches'].add(whs)
        rm_stats[rm]['dms'][dm]['neg_bills'] += 1
        rm_stats[rm]['dms'][dm]['loss'] += loss

    # Format RM & DM summaries
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
        pct = (s['total_loss'] / total_loss_sum * 100) if total_loss_sum > 0 else 0
        rm_summary_list.append({
            'rm_name': rm,
            'total_loss': round(s['total_loss'], 2),
            'loss_pct': round(pct, 1),
            'neg_bills_count': s['neg_bills'],
            'branches_affected': len(s['branches']),
            'dms': dm_list
        })
        
    # Format Top Items Loss
    top_items = []
    for iprod, item in item_loss_summary.items():
        top_items.append({
            'iprod': iprod,
            'name': item['name'],
            'total_qty': item['total_qty'],
            'bills_count': len(item['bills_count']),
            'normal_cost': item['normal_cost'],
            'normal_price': item['normal_price'],
            'total_line_cost': round(item['total_line_cost'], 2),
            'total_line_amt': round(item['total_line_amt'], 2)
        })
    top_items.sort(key=lambda x: x['bills_count'], reverse=True)
    
    # Format Top Branches Loss
    top_branches = list(branch_loss_summary.values())
    top_branches.sort(key=lambda x: x['total_loss'], reverse=True)
    
    # 7. Query Coupon Pairs (Bill 1: earned coupon vs Bill 2: redeemed coupon)
    cur.execute("""
        SELECT 
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
        WHERE b2.sodate = %s 
          AND (b2.sonetamt - b2.sototcost) < 0
          AND b2.sotowhs NOT IN ('901', '0901')
        ORDER BY b2.sotime DESC
    """, (target_date,))
    coupon_rows = cur.fetchall()

    coupon_pairs = []
    cp_profit_count = 0
    cp_loss_count = 0
    cp_split_count = 0
    total_b1_net = Decimal('0.00')
    total_b1_cost = Decimal('0.00')
    total_b1_gp = Decimal('0.00')
    total_b2_net = Decimal('0.00')
    total_b2_cost = Decimal('0.00')
    total_b2_gp = Decimal('0.00')

    for r in coupon_rows:
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
                        cp_split_count += 1
            except Exception:
                pass
        elif r['b1_date']:
            time_diff_str = f"คนละวัน ({str(r['b1_date'])[:10]})"
            
        br = branches_map.get(r['b2_whs'], {})
        branch_name = br.get('name', f"สาขา {r['b2_whs']}")
        rm = br.get('rm', '-')
        dm = br.get('dm', '-')
        
        if comb_gp >= 0:
            cp_profit_count += 1
        else:
            cp_loss_count += 1
            
        total_b1_net += b1_net
        total_b1_cost += b1_cost
        total_b1_gp += b1_gp
        total_b2_net += b2_net
        total_b2_cost += b2_cost
        total_b2_gp += b2_gp
        
        coupon_pairs.append({
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

    total_combined_net = total_b1_net + total_b2_net
    total_combined_cost = total_b1_cost + total_b2_cost
    total_combined_gp = total_combined_net - total_combined_cost
    overall_comb_gp_pct = round(total_combined_gp / total_combined_net * 100, 2) if total_combined_net > 0 else 0

    coupon_pair_summary = {
        'total_pairs': len(coupon_pairs),
        'net_profit_count': cp_profit_count,
        'net_profit_pct': round(cp_profit_count / len(coupon_pairs) * 100, 1) if coupon_pairs else 0,
        'net_loss_count': cp_loss_count,
        'net_loss_pct': round(cp_loss_count / len(coupon_pairs) * 100, 1) if coupon_pairs else 0,
        'split_bill_count': cp_split_count,
        'split_bill_pct': round(cp_split_count / len(coupon_pairs) * 100, 1) if coupon_pairs else 0,
        'total_b1_net': float(total_b1_net),
        'total_b1_cost': float(total_b1_cost),
        'total_b1_gp': float(total_b1_gp),
        'total_b2_net': float(total_b2_net),
        'total_b2_cost': float(total_b2_cost),
        'total_b2_gp': float(total_b2_gp),
        'total_combined_net': float(total_combined_net),
        'total_combined_cost': float(total_combined_cost),
        'total_combined_gp': float(total_combined_gp),
        'overall_comb_gp_pct': float(overall_comb_gp_pct)
    }

    # 8. Query Campaign Marketing Summary (From start_date e.g. 2026-11-01 to present)
    cur.execute("""
        SELECT 
            coupontype,
            couponamt,
            COUNT(*) as issued_cnt,
            SUM(couponamt) as issued_amt,
            SUM(CASE WHEN billuse IS NOT NULL AND TRIM(billuse) != '' THEN 1 ELSE 0 END) as used_cnt,
            SUM(CASE WHEN billuse IS NOT NULL AND TRIM(billuse) != '' THEN couponamt ELSE 0 END) as used_amt
        FROM customer_coupon
        WHERE begdate >= %s OR created_at >= %s
        GROUP BY coupontype, couponamt
        ORDER BY couponamt DESC
    """, (start_date, start_date))
    funnel_rows = cur.fetchall()
    
    total_issued_count = 0
    total_issued_amount = Decimal('0.00')
    total_used_count = 0
    total_used_amount = Decimal('0.00')
    funnel_list = []
    
    for fr in funnel_rows:
        i_cnt = int(fr['issued_cnt'] or 0)
        i_amt = Decimal(str(fr['issued_amt'] or 0))
        u_cnt = int(fr['used_cnt'] or 0)
        u_amt = Decimal(str(fr['used_amt'] or 0))
        red_pct = round(u_cnt / i_cnt * 100, 2) if i_cnt > 0 else 0
        total_issued_count += i_cnt
        total_issued_amount += i_amt
        total_used_count += u_cnt
        total_used_amount += u_amt
        funnel_list.append({
            'coupontype': fr['coupontype'],
            'couponamt': float(fr['couponamt']),
            'issued_cnt': i_cnt,
            'issued_amt': float(i_amt),
            'used_cnt': u_cnt,
            'used_amt': float(u_amt),
            'redemption_pct': float(red_pct)
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
            'total_sales_generated': float(tot_camp_sales),
            'total_cost': float(tot_camp_cost),
            'total_net_gp': float(tot_camp_gp),
            'net_gp_pct': float(camp_net_margin),
            'total_discount_spent': float(tot_camp_disc),
            'sales_roi_multiplier': float(sales_multiplier),
            'gp_roi_multiplier': float(gp_multiplier),
            'total_coupons_issued': total_issued_count,
            'total_issued_amount': float(total_issued_amount),
            'total_coupons_used': total_used_count,
            'total_used_amount': float(total_used_amount),
            'overall_redemption_rate': float(overall_redemption_rate),
            'unique_customers': int(unique_members_cnt)
        },
        'funnel': funnel_list,
        'timing_groups': timing_list
    }

    conn.close()
    
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'target_date': target_date,
        'filter_note': 'ไม่รวมสาขา 901',
        'summary': {
            'active_branches': overall['active_branches'],
            'total_bills': overall['total_bills'],
            'gross_sales': float(overall['gross_sales'] or 0),
            'total_discount': float(overall['total_discount'] or 0),
            'total_coupon': float(overall['total_coupon'] or 0),
            'total_score_disc': float(overall['total_score_disc'] or 0),
            'total_net_sales': float(overall['total_net_sales'] or 0),
            'total_cost': float(overall['total_cost'] or 0),
            'total_gp': float(overall['total_gp'] or 0),
            'gp_pct': round(float(overall['total_gp'] or 0) / float(overall['total_net_sales'] or 1) * 100, 2),
            'neg_bills_count': len(neg_bills),
            'neg_bills_pct': round(len(neg_bills) / float(overall['total_bills'] or 1) * 100, 2),
            'total_loss_amount': round(total_loss_sum, 2),
            'branches_affected_count': len(branch_loss_summary),
            'min_time': str(overall['min_time']),
            'max_time': str(overall['max_time'])
        },
        'rm_summary': rm_summary_list,
        'top_branches_affected': top_branches[:25],
        'top_loss_items': top_items[:20],
        'recent_negative_bills': neg_bills[:500],
        'negative_bills': neg_bills[:500],
        'coupon_pairs': coupon_pairs,
        'coupon_pair_summary': coupon_pair_summary,
        'campaign_summary': campaign_summary
    }
    
    json_str = json.dumps(payload, ensure_ascii=False, cls=DecimalEncoder, indent=2)
    
    # 1. Save JSON file
    out_json = os.path.join(BASE_DIR, "gp_anomaly_data.json")
    with open(out_json, "w", encoding="utf-8") as f:
        f.write(json_str)
    print(f"Data saved to {out_json}")
    
    # 2. Save JS file for local file:// protocol access without CORS restrictions!
    out_js = os.path.join(BASE_DIR, "gp_anomaly_data.js")
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = " + json_str + ";\n")
    print(f"Data saved to {out_js}")
    
    return payload

if __name__ == "__main__":
    res = run_detection()
    print("Summary (Excl 901):", res['summary'])
    print("RM count:", len(res['rm_summary']))
