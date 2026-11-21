import os
import json
import datetime
from decimal import Decimal
from detect_gp_anomalies import run_detection, DecimalEncoder

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def update_html_embedded_data(data):
    html_targets = [
        os.path.join(BASE_DIR, "gp_anomaly_dashboard.html"),
        os.path.join(BASE_DIR, "index.html")
    ]
    json_str = json.dumps(data, ensure_ascii=False, cls=DecimalEncoder)
    marker_start = "window.DASHBOARD_DATA = "
    marker_end = ";\n    </script>"

    for html_path in html_targets:
        if not os.path.exists(html_path):
            continue
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            
            start_idx = html.find(marker_start)
            if start_idx != -1:
                end_idx = html.find(marker_end, start_idx)
                if end_idx != -1:
                    new_html = html[:start_idx + len(marker_start)] + json_str + html[end_idx:]
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(new_html)
                    print(f"Embedded data in {os.path.basename(html_path)} updated successfully!")
        except Exception as e:
            print(f"Note: Could not embed in {os.path.basename(html_path)}:", e)

def check_and_alert():
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data = run_detection()
    update_html_embedded_data(data)
    
    s = data['summary']
    
    msg = f"""
=====================================================
🚨 แจ้งเตือนบิลขายผิดปกติ (GP ติดลบ) ประจำวันที่ {data['target_date']}
🕒 เวลาตรวจเช็ค: {now_str}
=====================================================
📊 ภาพรวมการขายทุกสาขาวันนี้:
  • สาขาที่เปิดขาย: {s['active_branches']} สาขา (รวม {s['total_bills']:,} บิล)
  • ยอดขายสุทธิรวม: ฿{s['total_net_sales']:,.2f} บาท
  • กำไรขั้นต้นรวม: ฿{s['total_gp']:,.2f} บาท ({s['gp_pct']}%)

⚠️ รายการบิลวิกฤต (ขายขาดทุน / GP ติดลบ):
  • จำนวนบิลที่ GP ติดลบ: {s['neg_bills_count']:,} บิล ({s['neg_bills_pct']}% ของบิลทั้งหมด)
  • กระจายตัวใน: {s['branches_affected_count']} สาขา
  • ยอดขาดทุนสะสมรวม: -฿{s['total_loss_amount']:,.2f} บาท

🏢 5 อันดับสาขาที่พบยอดขาดทุนสะสมสูงสุด:"""

    for idx, b in enumerate(data['top_branches_affected'][:5], 1):
        msg += f"\n  {idx}. สาขา {b['branch_code']} ({b['branch_name']}) - ขาดทุนสะสม -฿{b['total_loss']:,.2f} ({b['neg_bills_count']} บิล) [จ.{b['province']}]"

    msg += "\n\n📦 5 อันดับสินค้าที่เป็นตัวการดึงให้ GP ติดลบ:"
    for idx, it in enumerate(data['top_loss_items'][:5], 1):
        msg += f"\n  {idx}. {it['name']} (รหัส {it['iprod']}) - พบใน {it['bills_count']} บิลขาดทุน (รวม {it['total_qty']} ชิ้น)"

    msg += f"""
=====================================================
🌐 เปิดดูแดชบอร์ดมอนิเตอร์แบบ Interactive ได้ที่:
file:///f:/lease/gp_anomaly_dashboard.html
=====================================================
"""
    print(msg)
    
    log_path = os.path.join(BASE_DIR, "alert_history.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
        
    return msg

if __name__ == "__main__":
    check_and_alert()
