"""
每周股票池轮动扫描器 v3
行业板块 + 概念板块双扫描
综合评分选股 + 动态名额分配
固定仓11只 + 轮动仓18只
"""

import akshare as ak
import pandas as pd
import requests
import json
import os
import sys
from datetime import datetime

# ==========================================
# 配置参数
# ==========================================
MIN_DAILY_AMOUNT = 3e8       # 日均成交额 >= 3亿
PRICE_MIN = 5                # 股价下限
PRICE_MAX = 150              # 股价上限
MIN_MARKET_CAP = 50e8        # 最小市值 50亿
MAX_REPLACE = 18             # 轮动仓最大数量
TOP_SECTORS = 12             # 扫描最强板块数量（行业+概念合并后取前12）

# 综合评分权重
WEIGHT_CHANGE = 0.4          # 涨幅权重
WEIGHT_AMOUNT = 0.35         # 成交额权重
WEIGHT_TURNOVER = 0.25       # 换手率权重

# 排除科创板（688）和北交所（8开头）
EXCLUDE_PREFIX = ['688', '8']
EXCLUDE_KEYWORDS = ['ST', '*ST']

# 固定仓（11只，不参与轮动）
FIXED_POOL = [
    '300308',  # 中际旭创 - 算力/光模块龙头
    '600406',  # 国电南瑞 - 电网龙头
    '002179',  # 中航光电 - 军工连接器龙头
    '002230',  # 科大讯飞 - AI应用龙头
    '600875',  # 东方电气 - 风电/核聚变龙头
    '603986',  # 兆易创新 - 存储芯片龙头
    '002371',  # 北方华创 - 半导体设备龙头
    '600498',  # 烽火通信 - 5G/6G龙头
    '600118',  # 中国卫星 - 商业航天龙头
    '603123',  # 翠微股份 - 互联网金融
    '601211',  # 国泰君安 - 券商/大盘金融
]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def get_sector_rankings():
    """获取行业板块 + 概念板块，合并排名"""
    log("📊 获取板块数据...")
    
    all_sectors = []
    
    # 1. 行业板块
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and len(df) > 0:
            log(f"  行业板块: {len(df)} 个")
            for _, row in df.iterrows():
                try:
                    name = str(row['板块名称'])
                    change_pct = float(row['涨跌幅'])
                    all_sectors.append({
                        'name': name,
                        'change_pct': change_pct,
                        'type': '行业',
                        'leader': str(row.get('领涨股票', '')),
                    })
                except (ValueError, KeyError, TypeError):
                    continue
    except Exception as e:
        log(f"  ⚠️ 行业板块获取异常: {e}")
    
    # 2. 概念板块
    try:
        df2 = ak.stock_board_concept_name_em()
        if df2 is not None and len(df2) > 0:
            log(f"  概念板块: {len(df2)} 个")
            for _, row in df2.iterrows():
                try:
                    name = str(row['板块名称'])
                    change_pct = float(row['涨跌幅'])
                    all_sectors.append({
                        'name': name,
                        'change_pct': change_pct,
                        'type': '概念',
                        'leader': str(row.get('领涨股票', '')),
                    })
                except (ValueError, KeyError, TypeError):
                    continue
    except Exception as e:
        log(f"  ⚠️ 概念板块获取异常: {e}")
    
# 排除投机性概念板块
    EXCLUDE_SECTOR_KEYWORDS = ['连板', '打板', '涨停', '首板', '二板', '三板', '跌停', '摘帽', '复牌', '破板', '炸板']
    all_sectors = [s for s in all_sectors if not any(kw in s['name'] for kw in EXCLUDE_SECTOR_KEYWORDS)]
    
    # 合并排序
    all_sectors.sort(key=lambda x: x['change_pct'], reverse=True)
    top_sectors = all_sectors[:TOP_SECTORS]
    
    log(f"\n  🏆 综合排名前 {len(top_sectors)} 个板块:")
    for i, s in enumerate(top_sectors, 1):
        tag = "🏭" if s['type'] == '行业' else "💡"
        quota = get_quota(i, len(top_sectors))
        log(f"    {i}. {tag} {s['name']}({s['type']}): {s['change_pct']:+.2f}% | 领涨: {s['leader']} | 名额: {quota}只")
    
    return top_sectors

def get_quota(rank, total):
    """根据板块排名动态分配名额：前3给3只，4-6给2只，7-12给1只"""
    if rank <= 3:
        return 3
    elif rank <= 6:
        return 2
    else:
        return 1

def get_sector_stocks(sector_name, sector_type):
    """获取板块成分股"""
    try:
        if sector_type == '行业':
            df = ak.stock_board_industry_cons_em(symbol=sector_name)
        else:
            df = ak.stock_board_concept_cons_em(symbol=sector_name)
        if df is not None and len(df) > 0:
            return df
    except Exception as e:
        log(f"  ⚠️ 获取 {sector_name} 成分股异常: {e}")
    return pd.DataFrame()

def filter_stock(code, name, price, amount, market_cap):
    """单只股票过滤"""
    code_str = str(code).zfill(6)
    
    for prefix in EXCLUDE_PREFIX:
        if code_str.startswith(prefix):
            return False
    
    for kw in EXCLUDE_KEYWORDS:
        if kw in str(name):
            return False
    
    if code_str in FIXED_POOL:
        return False
    
    try:
        p = float(price)
        if p < PRICE_MIN or p > PRICE_MAX:
            return False
    except (ValueError, TypeError):
        return False
    
    try:
        amt = float(amount)
        if amt < MIN_DAILY_AMOUNT:
            return False
    except (ValueError, TypeError):
        return False
    
    try:
        cap = float(market_cap)
        if cap > 0 and cap < MIN_MARKET_CAP:
            return False
    except (ValueError, TypeError):
        pass
    
    return True

def compute_score(change_pct, amount, turnover):
    """
    综合评分：涨幅 40% + 成交额 35% + 换手率 25%
    """
    score = 0
    score += float(change_pct) * WEIGHT_CHANGE
    score += float(amount) / 1e8 * WEIGHT_AMOUNT
    score += float(turnover) * WEIGHT_TURNOVER
    return round(score, 2)

def scan_sector_leaders(top_sectors):
    """从强势板块中筛选龙头股，综合评分 + 动态名额"""
    log("\n🔍 开始筛选板块龙头...")
    
    candidates = []
    seen_codes = set()
    
    for rank, sector in enumerate(top_sectors, 1):
        sector_name = sector['name']
        sector_type = sector['type']
        quota = get_quota(rank, len(top_sectors))
        tag = "🏭" if sector_type == '行业' else "💡"
        
        log(f"\n  {tag} [{rank}/{len(top_sectors)}] {sector_name}({sector_type}) | 涨幅:{sector['change_pct']:+.2f}% | 名额:{quota}只")
        
        df = get_sector_stocks(sector_name, sector_type)
        if df.empty:
            log(f"    跳过（无成分股数据）")
            continue
        
        sector_picks = []
        for _, row in df.iterrows():
            try:
                code = str(row.get('代码', row.iloc[1])).zfill(6)
                name = str(row.get('名称', row.iloc[2]))
                price = float(row.get('最新价', 0))
                change_pct = float(row.get('涨跌幅', 0))
                amount = float(row.get('成交额', 0))
                turnover = float(row.get('换手率', 0))
                market_cap = float(row.get('总市值', 0))
                
                if code in seen_codes:
                    continue
                
                if not filter_stock(code, name, price, amount, market_cap):
                    continue
                
                score = compute_score(change_pct, amount, turnover)
                
                sector_picks.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'change_pct': change_pct,
                    'amount': amount,
                    'turnover': turnover,
                    'market_cap': market_cap,
                    'sector': sector_name,
                    'sector_type': sector_type,
                    'sector_change': sector['change_pct'],
                    'sector_rank': rank,
                    'score': score,
                })
            except (ValueError, IndexError, TypeError):
                continue
        
        # 按综合评分排序，取名额数量
        sector_picks.sort(key=lambda x: x['score'], reverse=True)
        for pick in sector_picks[:quota]:
            seen_codes.add(pick['code'])
            candidates.append(pick)
            log(f"    ✅ {pick['code']} {pick['name']} | 评分:{pick['score']:.1f} | 涨幅:{pick['change_pct']:+.2f}% | 成交:{pick['amount']/1e8:.1f}亿 | 换手:{pick['turnover']:.1f}%")
    
    return candidates

def generate_report(candidates, top_sectors):
    """生成推送报告"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    report = f"# 📡 每周股票池轮动扫描 v3\n"
    report += f"**扫描时间**: {now}\n"
    report += f"**固定仓**: {len(FIXED_POOL)}只 | **轮动仓上限**: {MAX_REPLACE}只\n\n"
    
    # 板块概览
    report += "## 🔥 本周最强板块（行业+概念综合排名）\n"
    for i, s in enumerate(top_sectors, 1):
        tag = "🏭" if s['type'] == '行业' else "💡"
        quota = get_quota(i, len(top_sectors))
        medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        report += f"{medal} {tag} **{s['name']}**({s['type']}) {s['change_pct']:+.2f}% → {quota}只名额\n"
    report += "\n"
    
    # 候选股票
    report += "## 🎯 轮动仓候选（综合评分排序）\n"
    report += "*评分 = 涨幅×40% + 成交额×35% + 换手率×25%*\n\n"
    
    current_sector = ""
    for c in candidates:
        if c['sector'] != current_sector:
            current_sector = c['sector']
            tag = "🏭" if c['sector_type'] == '行业' else "💡"
            report += f"\n**{tag}【{current_sector}】** 板块涨幅 {c['sector_change']:+.2f}%\n"
        
        cap_str = f"{c['market_cap']/1e8:.0f}亿" if c['market_cap'] > 0 else "N/A"
        report += f"- `{c['code']}` **{c['name']}** | ⭐{c['score']:.1f}分 | 💰{c['price']:.2f}元 | 📈{c['change_pct']:+.2f}% | 成交{c['amount']/1e8:.1f}亿 | 换手{c['turnover']:.1f}% | 市值{cap_str}\n"
    
    report += f"\n---\n"
    report += f"**共筛选出 {len(candidates)} 只候选** | 轮动仓取前 {MAX_REPLACE} 只\n"
    report += f"筛选条件: 成交额≥3亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥50亿 | 排除ST/科创板/北交所\n"
    report += f"名额分配: 排名1-3板块各3只 | 排名4-6各2只 | 排名7-12各1只\n\n"
    
    # 代码汇总
    if candidates:
        rotation_codes = [c['code'] for c in candidates[:MAX_REPLACE]]
        report += f"## 📋 代码汇总（可直接复制）\n\n"
        
        report += f"**轮动仓（{len(rotation_codes)}只）**:\n"
        report += f"`{','.join(rotation_codes)}`\n\n"
        
        report += f"**固定仓（{len(FIXED_POOL)}只）**:\n"
        report += f"`{','.join(FIXED_POOL)}`\n\n"
        
        # 合并完整池
        all_codes = FIXED_POOL + rotation_codes
        report += f"**完整股票池（{len(all_codes)}只，可直接替换到 daily_analysis.yml）**:\n"
        report += f"`{','.join(all_codes)}`\n\n"
        
        # 三批分配建议
        batch_size = len(all_codes) // 3
        remainder = len(all_codes) % 3
        b1_end = batch_size + (1 if remainder > 0 else 0)
        b2_end = b1_end + batch_size + (1 if remainder > 1 else 0)
        
        batch1 = all_codes[:b1_end]
        batch2 = all_codes[b1_end:b2_end]
        batch3 = all_codes[b2_end:]
        
        report += f"**三批分配建议**:\n"
        report += f"- 第一批（{len(batch1)}只）: `{','.join(batch1)}`\n"
        report += f"- 第二批（{len(batch2)}只）: `{','.join(batch2)}`\n"
        report += f"- 第三批（{len(batch3)}只）: `{','.join(batch3)}`\n"
    
    report += f"\n> ⚠️ 以上为自动筛选结果，仅供参考。建议对比自身判断后决定是否替换。"
    
    return report

def push_to_pushplus(report):
    """推送到 PushPlus"""
    token = os.environ.get('PUSHPLUS_TOKEN', '')
    if not token:
        log("⚠️ 未配置 PUSHPLUS_TOKEN，跳过推送")
        return False
    
    data = {
        'token': token,
        'title': f'📡 每周轮动扫描 {datetime.now().strftime("%m/%d")}',
        'content': report,
        'template': 'markdown',
    }
    
    try:
        resp = requests.post('http://www.pushplus.plus/send', json=data, timeout=30)
        result = resp.json()
        if result.get('code') == 200:
            log("✅ PushPlus 推送成功")
            return True
        else:
            log(f"❌ PushPlus 推送失败: {result}")
            return False
    except Exception as e:
        log(f"❌ PushPlus 推送异常: {e}")
        return False

def main():
    log("=" * 50)
    log("📡 每周股票池轮动扫描器 v3 启动")
    log("=" * 50)
    log(f"固定仓: {len(FIXED_POOL)} 只 | 轮动仓上限: {MAX_REPLACE} 只 | 总容量: {len(FIXED_POOL) + MAX_REPLACE} 只")
    log(f"扫描板块数: {TOP_SECTORS} | 参数: 成交额≥{MIN_DAILY_AMOUNT/1e8:.0f}亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥{MIN_MARKET_CAP/1e8:.0f}亿")
    log(f"评分权重: 涨幅{WEIGHT_CHANGE*100:.0f}% + 成交额{WEIGHT_AMOUNT*100:.0f}% + 换手率{WEIGHT_TURNOVER*100:.0f}%")
    log(f"名额规则: TOP1-3→3只 | TOP4-6→2只 | TOP7-12→1只")
    log("")
    
    # 第一步：获取强势板块（行业+概念）
    top_sectors = get_sector_rankings()
    if not top_sectors:
        log("❌ 未获取到板块数据，退出")
        sys.exit(1)
    
    # 第二步：综合评分筛选龙头
    candidates = scan_sector_leaders(top_sectors)
    log(f"\n📋 共筛选出 {len(candidates)} 只候选股票")
    
    if not candidates:
        log("⚠️ 未筛选出候选股票，检查筛选条件是否过严")
    
    # 第三步：生成报告
    report = generate_report(candidates, top_sectors)
    
    # 第四步：保存报告
    os.makedirs('reports', exist_ok=True)
    report_file = f"reports/weekly_scan_{datetime.now().strftime('%Y%m%d')}.md"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    log(f"\n📄 报告已保存: {report_file}")
    
    # 第五步：推送
    push_to_pushplus(report)
    
    log("\n✅ 扫描完成")

if __name__ == '__main__':
    main()
