"""
每周股票池轮动扫描器
自动筛选本周最强板块龙头，生成轮动仓替换建议
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
MAX_CONTINUOUS_LIMIT_UP = 5  # 连续涨停天数上限
MAX_REPLACE = 10             # 每周最多替换数量
TOP_SECTORS = 8              # 扫描最强板块数量
STOCKS_PER_SECTOR = 2        # 每个板块取几只

# 排除科创板（688）和 ST
EXCLUDE_PREFIX = ['688', '8']
EXCLUDE_KEYWORDS = ['ST', '*ST', 'N ', 'C ']

# 固定仓（不参与轮动的核心票）
FIXED_POOL = [
    '300308',  # 中际旭创 - 光模块龙头
    '300502',  # 新易盛 - 光模块
    '600406',  # 国电南瑞 - 电网龙头
    '002230',  # 科大讯飞 - AI应用龙头
    '002049',  # 紫光国微 - 军工芯片
    '601727',  # 上海电气 - 电力/核聚变
    '600875',  # 东方电气 - 风电/核聚变
    '002179',  # 中航光电 - 军工连接器
    '600900',  # 长江电力 - 电力压舱石
    '002371',  # 北方华创 - 半导体设备
    '600309',  # 万华化学 - 化工龙头
    '002202',  # 金风科技 - 风电龙头
    '601698',  # 中国卫通 - 商业航天
    '002281',  # 光迅科技 - 光模块/5G
    '600498',  # 烽火通信 - 5G/6G
    '601985',  # 中国核电 - 核电龙头
    '603986',  # 兆易创新 - 存储芯片
    '002261',  # 拓维信息 - 算力
    '601868',  # 中国能建 - 央国企/电网
]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def get_sector_rankings():
    """获取板块涨幅排名和资金流向"""
    log("📊 获取板块数据...")
    
    sectors = []
    
    try:
        # 行业板块涨幅排名
        df_sector = ak.stock_board_industry_name_em()
        if df_sector is not None and len(df_sector) > 0:
            # 标准化列名
            cols = df_sector.columns.tolist()
            df_sector.columns = [str(c).strip() for c in cols]
            log(f"  获取到 {len(df_sector)} 个行业板块")
            log(f"  列名: {df_sector.columns.tolist()}")
            log(f"  前3行: {df_sector.head(3).to_string()}")
            
            # 提取板块名称和涨幅
            for _, row in df_sector.head(TOP_SECTORS * 2).iterrows():
                try:
                    name = str(row.iloc[1]) if len(row) > 1 else str(row.iloc[0])
                    change_pct = float(row.iloc[2]) if len(row) > 2 else 0
                    sectors.append({
                        'name': name,
                        'change_pct': change_pct,
                    })
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        log(f"  ⚠️ 获取板块数据异常: {e}")
    
    # 按涨幅排序取前N个
    sectors.sort(key=lambda x: x['change_pct'], reverse=True)
    top_sectors = sectors[:TOP_SECTORS]
    
    log(f"  本周最强 {len(top_sectors)} 个板块:")
    for s in top_sectors:
        log(f"    {s['name']}: {s['change_pct']:+.2f}%")
    
    return top_sectors

def get_sector_stocks(sector_name):
    """获取指定板块的成分股"""
    try:
        df = ak.stock_board_industry_cons_em(symbol=sector_name)
        if df is not None and len(df) > 0:
            return df
    except Exception as e:
        log(f"  ⚠️ 获取 {sector_name} 成分股异常: {e}")
    return pd.DataFrame()

def filter_stock(code, name, price, amount, market_cap):
    """单只股票过滤"""
    code_str = str(code).zfill(6)
    
    # 排除科创板
    for prefix in EXCLUDE_PREFIX:
        if code_str.startswith(prefix):
            return False, "科创板/北交所"
    
    # 排除ST
    for kw in EXCLUDE_KEYWORDS:
        if kw in str(name):
            return False, "ST/次新"
    
    # 排除已在固定仓的
    if code_str in FIXED_POOL:
        return False, "已在固定仓"
    
    # 股价过滤
    try:
        p = float(price)
        if p < PRICE_MIN or p > PRICE_MAX:
            return False, f"股价{p}不在{PRICE_MIN}-{PRICE_MAX}区间"
    except (ValueError, TypeError):
        return False, "股价数据异常"
    
    # 成交额过滤
    try:
        amt = float(amount)
        if amt < MIN_DAILY_AMOUNT:
            return False, f"成交额{amt/1e8:.1f}亿 < 3亿"
    except (ValueError, TypeError):
        return False, "成交额数据异常"
    
    # 市值过滤
    try:
        cap = float(market_cap)
        if cap > 0 and cap < MIN_MARKET_CAP:
            return False, f"市值{cap/1e8:.0f}亿 < 50亿"
    except (ValueError, TypeError):
        pass  # 市值数据缺失不过滤
    
    return True, "通过"

def scan_sector_leaders(top_sectors):
    """从强势板块中筛选龙头股"""
    log("\n🔍 开始筛选板块龙头...")
    
    candidates = []
    seen_codes = set()
    
    for sector in top_sectors:
        sector_name = sector['name']
        log(f"\n  扫描板块: {sector_name} ({sector['change_pct']:+.2f}%)")
        
        df = get_sector_stocks(sector_name)
        if df.empty:
            log(f"    跳过（无成分股数据）")
            continue
        
        # 尝试识别关键列
        cols = df.columns.tolist()
        log(f"    成分股 {len(df)} 只，列: {cols[:8]}")
        
        sector_picks = []
        for _, row in df.iterrows():
            try:
                code = str(row.iloc[1]).zfill(6) if len(row) > 1 else str(row.iloc[0]).zfill(6)
                name = str(row.iloc[2]) if len(row) > 2 else "未知"
                price = float(row.iloc[3]) if len(row) > 3 else 0
                change_pct = float(row.iloc[4]) if len(row) > 4 else 0
                amount = float(row.iloc[5]) if len(row) > 5 else 0
                market_cap = float(row.iloc[8]) if len(row) > 8 else 0
                
                if code in seen_codes:
                    continue
                
                passed, reason = filter_stock(code, name, price, amount, market_cap)
                if not passed:
                    continue
                
                sector_picks.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'change_pct': change_pct,
                    'amount': amount,
                    'market_cap': market_cap,
                    'sector': sector_name,
                    'sector_change': sector['change_pct'],
                })
            except (ValueError, IndexError, TypeError):
                continue
        
        # 按成交额排序，取前N只
        sector_picks.sort(key=lambda x: x['amount'], reverse=True)
        for pick in sector_picks[:STOCKS_PER_SECTOR]:
            seen_codes.add(pick['code'])
            candidates.append(pick)
            log(f"    ✅ {pick['code']} {pick['name']} | 价格:{pick['price']:.2f} | 涨幅:{pick['change_pct']:+.2f}% | 成交额:{pick['amount']/1e8:.1f}亿")
    
    return candidates

def generate_report(candidates, top_sectors):
    """生成推送报告"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    report = f"# 📡 每周股票池轮动扫描\n"
    report += f"**扫描时间**: {now}\n\n"
    
    # 板块概览
    report += "## 🔥 本周最强板块\n"
    for i, s in enumerate(top_sectors, 1):
        emoji = "🥇🥈🥉"[i-1] if i <= 3 else "🔸"
        report += f"{emoji} **{s['name']}** {s['change_pct']:+.2f}%\n"
    report += "\n"
    
    # 候选股票
    report += "## 🎯 轮动仓候选（按板块分组）\n"
    
    current_sector = ""
    for c in candidates:
        if c['sector'] != current_sector:
            current_sector = c['sector']
            report += f"\n**【{current_sector}】** 板块涨幅 {c['sector_change']:+.2f}%\n"
        
        cap_str = f"{c['market_cap']/1e8:.0f}亿" if c['market_cap'] > 0 else "N/A"
        report += f"- `{c['code']}` **{c['name']}** | 💰{c['price']:.2f}元 | 📈{c['change_pct']:+.2f}% | 成交{c['amount']/1e8:.1f}亿 | 市值{cap_str}\n"
    
    report += f"\n---\n"
    report += f"**共筛选出 {len(candidates)} 只候选** | 最多替换 {MAX_REPLACE} 只\n"
    report += f"筛选条件: 日均成交额≥3亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥50亿 | 排除ST/科创板\n\n"
    
    # 候选代码汇总（方便直接复制）
    if candidates:
        codes = [c['code'] for c in candidates[:MAX_REPLACE]]
        report += f"**轮动仓候选代码（可直接复制）**:\n"
        report += f"`{','.join(codes)}`\n\n"
        
        report += f"**固定仓代码（不动）**:\n"
        report += f"`{','.join(FIXED_POOL)}`\n"
    
    report += f"\n> ⚠️ 以上为自动筛选结果，仅供参考，请结合自身判断决定是否替换。"
    
    return report

def push_to_pushplus(report):
    """推送到 PushPlus"""
    token = os.environ.get('PUSHPLUS_TOKEN', '')
    if not token:
        log("⚠️ 未配置 PUSHPLUS_TOKEN，跳过推送")
        return False
    
    data = {
        'token': token,
        'title': f'📡 每周股票池轮动扫描 {datetime.now().strftime("%m/%d")}',
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
    log("📡 每周股票池轮动扫描器 启动")
    log("=" * 50)
    log(f"参数: 成交额≥{MIN_DAILY_AMOUNT/1e8:.0f}亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥{MIN_MARKET_CAP/1e8:.0f}亿")
    log(f"固定仓: {len(FIXED_POOL)} 只 | 最多替换: {MAX_REPLACE} 只")
    log("")
    
    # 第一步：获取强势板块
    top_sectors = get_sector_rankings()
    if not top_sectors:
        log("❌ 未获取到板块数据，退出")
        sys.exit(1)
    
    # 第二步：筛选板块龙头
    candidates = scan_sector_leaders(top_sectors)
    log(f"\n📋 共筛选出 {len(candidates)} 只候选股票")
    
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
