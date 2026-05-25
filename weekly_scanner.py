"""
每周股票池轮动扫描器 v5
全市场个股扫描模式 - 不依赖板块接口
直接从全A股里按综合评分筛选龙头
固定仓11只 + 轮动仓18只
"""

import akshare as ak
import pandas as pd
import requests
import os
import sys
import time
from datetime import datetime

# ==========================================
# 配置参数
# ==========================================
MIN_DAILY_AMOUNT = 3e8       # 日均成交额 >= 3亿
PRICE_MIN = 5                # 股价下限
PRICE_MAX = 150              # 股价上限
MIN_MARKET_CAP = 50e8        # 最小市值 50亿
MAX_REPLACE = 18             # 轮动仓最大数量

# 综合评分权重
WEIGHT_CHANGE = 0.4          # 涨幅权重
WEIGHT_AMOUNT = 0.35         # 成交额权重
WEIGHT_TURNOVER = 0.25       # 换手率权重

# 排除科创板（688）和北交所（8开头）
EXCLUDE_PREFIX = ['688', '8']
EXCLUDE_KEYWORDS = ['ST', '*ST', 'N ', 'C ']

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

def retry_call(func, max_retries=3, delay=5):
    """带重试的函数调用"""
    for i in range(max_retries):
        try:
            return func()
        except Exception as e:
            if i < max_retries - 1:
                log(f"  ⚠️ 第{i+1}次请求失败，{delay}秒后重试: {e}")
                time.sleep(delay)
            else:
                raise e

def fetch_all_stocks():
    """获取全市场A股实时行情"""
    log("📊 获取全市场A股行情...")
    
    # 尝试多个数据源
    sources = [
        ("东方财富", lambda: ak.stock_zh_a_spot_em()),
        ("腾讯", lambda: ak.stock_zh_a_spot()),
    ]
    
    for name, func in sources:
        try:
            log(f"  尝试 {name}...")
            df = retry_call(func)
            if df is not None and len(df) > 0:
                log(f"  ✅ {name}: 获取到 {len(df)} 只股票")
                log(f"  列名: {df.columns.tolist()[:15]}")
                return df, name
        except Exception as e:
            log(f"  ❌ {name} 失败: {e}")
            continue
    
    return None, None

def parse_stock_data(df, source_name):
    """解析不同数据源的股票数据为统一格式"""
    stocks = []
    
    for _, row in df.iterrows():
        try:
            # 东方财富格式
            if source_name == "东方财富":
                code = str(row.get('代码', '')).zfill(6)
                name = str(row.get('名称', ''))
                price = float(row.get('最新价', 0) or 0)
                change_pct = float(row.get('涨跌幅', 0) or 0)
                amount = float(row.get('成交额', 0) or 0)
                turnover = float(row.get('换手率', 0) or 0)
                market_cap = float(row.get('总市值', 0) or 0)
                volume_ratio = float(row.get('量比', 0) or 0)
            # 腾讯格式
            elif source_name == "腾讯":
                code = str(row.get('code', row.get('代码', ''))).zfill(6)
                name = str(row.get('name', row.get('名称', '')))
                price = float(row.get('price', row.get('最新价', 0)) or 0)
                change_pct = float(row.get('changepercent', row.get('涨跌幅', 0)) or 0)
                amount = float(row.get('amount', row.get('成交额', 0)) or 0)
                turnover = float(row.get('turnoverratio', row.get('换手率', 0)) or 0)
                market_cap = float(row.get('mktcap', row.get('总市值', 0)) or 0)
                volume_ratio = 0
            else:
                continue
            
            if not code or price <= 0:
                continue
                
            stocks.append({
                'code': code,
                'name': name,
                'price': price,
                'change_pct': change_pct,
                'amount': amount,
                'turnover': turnover,
                'market_cap': market_cap,
                'volume_ratio': volume_ratio,
            })
        except (ValueError, TypeError, KeyError):
            continue
    
    return stocks

def filter_stock(stock):
    """过滤单只股票"""
    code = stock['code']
    name = stock['name']
    
    # 排除科创板/北交所
    for prefix in EXCLUDE_PREFIX:
        if code.startswith(prefix):
            return False
    
    # 排除ST/次新
    for kw in EXCLUDE_KEYWORDS:
        if kw in name:
            return False
    
    # 排除固定仓
    if code in FIXED_POOL:
        return False
    
    # 股价过滤
    if stock['price'] < PRICE_MIN or stock['price'] > PRICE_MAX:
        return False
    
    # 成交额过滤
    if stock['amount'] < MIN_DAILY_AMOUNT:
        return False
    
    # 市值过滤
    if stock['market_cap'] > 0 and stock['market_cap'] < MIN_MARKET_CAP:
        return False
    
    return True

def compute_score(stock):
    """综合评分"""
    score = 0
    score += stock['change_pct'] * WEIGHT_CHANGE
    score += stock['amount'] / 1e8 * WEIGHT_AMOUNT
    score += stock['turnover'] * WEIGHT_TURNOVER
    return round(score, 2)

def scan_market(stocks):
    """全市场扫描，筛选并排序"""
    log("\n🔍 开始全市场筛选...")
    
    # 过滤
    filtered = [s for s in stocks if filter_stock(s)]
    log(f"  过滤后剩余: {len(filtered)} 只（总共 {len(stocks)} 只）")
    
    # 计算评分
    for s in filtered:
        s['score'] = compute_score(s)
    
    # 按评分排序
    filtered.sort(key=lambda x: x['score'], reverse=True)
    
    # 取前MAX_REPLACE只
    candidates = filtered[:MAX_REPLACE]
    
    log(f"  🎯 选出 {len(candidates)} 只候选:")
    for i, c in enumerate(candidates, 1):
        log(f"    {i}. {c['code']} {c['name']} | 评分:{c['score']:.1f} | 涨幅:{c['change_pct']:+.2f}% | 成交:{c['amount']/1e8:.1f}亿 | 换手:{c['turnover']:.1f}%")
    
    return candidates

def get_market_summary(stocks):
    """生成市场概况"""
    total = len(stocks)
    up = len([s for s in stocks if s['change_pct'] > 0])
    down = len([s for s in stocks if s['change_pct'] < 0])
    flat = total - up - down
    
    avg_change = sum(s['change_pct'] for s in stocks) / total if total > 0 else 0
    total_amount = sum(s['amount'] for s in stocks)
    
    # 涨幅前10的板块方向（用个股名称关键词粗略判断）
    top_stocks = sorted(stocks, key=lambda x: x['change_pct'], reverse=True)[:30]
    
    return {
        'total': total,
        'up': up,
        'down': down,
        'flat': flat,
        'avg_change': avg_change,
        'total_amount': total_amount,
        'top_stocks': top_stocks,
    }

def generate_report(candidates, market_summary, source_name):
    """生成推送报告"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    report = f"# 📡 每周股票池轮动扫描 v5\n"
    report += f"**扫描时间**: {now}\n"
    report += f"**数据源**: {source_name}\n"
    report += f"**固定仓**: {len(FIXED_POOL)}只 | **轮动仓上限**: {MAX_REPLACE}只\n\n"
    
    # 市场概况
    ms = market_summary
    report += "## 📈 市场概况\n"
    report += f"上涨 **{ms['up']}** 只 | 下跌 **{ms['down']}** 只 | 平盘 {ms['flat']} 只\n"
    report += f"平均涨幅 **{ms['avg_change']:+.2f}%** | 全市场成交 **{ms['total_amount']/1e8:.0f}亿**\n\n"
    
    # 今日最强个股TOP10
    report += "## 🔥 今日最强个股 TOP10\n"
    for i, s in enumerate(ms['top_stocks'][:10], 1):
        medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        report += f"{medal} **{s['name']}**({s['code']}) {s['change_pct']:+.2f}% | 成交{s['amount']/1e8:.1f}亿\n"
    report += "\n"
    
    # 候选股票
    report += "## 🎯 轮动仓候选（综合评分 TOP18）\n"
    report += "*评分 = 涨幅×40% + 成交额×35% + 换手率×25%*\n\n"
    
    for i, c in enumerate(candidates, 1):
        cap_str = f"{c['market_cap']/1e8:.0f}亿" if c['market_cap'] > 0 else "N/A"
        report += f"{i}. `{c['code']}` **{c['name']}** | ⭐{c['score']:.1f}分 | 💰{c['price']:.2f}元 | 📈{c['change_pct']:+.2f}% | 成交{c['amount']/1e8:.1f}亿 | 换手{c['turnover']:.1f}% | 市值{cap_str}\n"
    
    report += f"\n---\n"
    report += f"筛选条件: 成交额≥3亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥50亿 | 排除ST/科创板/北交所\n\n"
    
    # 代码汇总
    if candidates:
        rotation_codes = [c['code'] for c in candidates]
        report += f"## 📋 代码汇总（可直接复制）\n\n"
        
        report += f"**轮动仓（{len(rotation_codes)}只）**:\n"
        report += f"`{','.join(rotation_codes)}`\n\n"
        
        report += f"**固定仓（{len(FIXED_POOL)}只）**:\n"
        report += f"`{','.join(FIXED_POOL)}`\n\n"
        
        all_codes = FIXED_POOL + rotation_codes
        report += f"**完整股票池（{len(all_codes)}只）**:\n"
        report += f"`{','.join(all_codes)}`\n\n"
        
        # 三批分配
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
    log("📡 每周股票池轮动扫描器 v5 启动")
    log("=" * 50)
    log(f"模式: 全市场个股扫描（不依赖板块接口）")
    log(f"固定仓: {len(FIXED_POOL)} 只 | 轮动仓上限: {MAX_REPLACE} 只 | 总容量: {len(FIXED_POOL) + MAX_REPLACE} 只")
    log(f"参数: 成交额≥{MIN_DAILY_AMOUNT/1e8:.0f}亿 | 股价{PRICE_MIN}-{PRICE_MAX}元 | 市值≥{MIN_MARKET_CAP/1e8:.0f}亿")
    log(f"评分: 涨幅{WEIGHT_CHANGE*100:.0f}% + 成交额{WEIGHT_AMOUNT*100:.0f}% + 换手率{WEIGHT_TURNOVER*100:.0f}%")
    log("")
    
    # 第一步：获取全市场行情
    df, source_name = fetch_all_stocks()
    if df is None:
        log("❌ 无法获取市场数据，退出")
        sys.exit(1)
    
    # 第二步：解析数据
    stocks = parse_stock_data(df, source_name)
    log(f"  解析有效股票: {len(stocks)} 只")
    
    if not stocks:
        log("❌ 无有效股票数据，退出")
        sys.exit(1)
    
    # 第三步：市场概况
    market_summary = get_market_summary(stocks)
    log(f"\n📈 市场概况: 上涨{market_summary['up']} 下跌{market_summary['down']} 平均涨幅{market_summary['avg_change']:+.2f}%")
    
    # 第四步：全市场筛选
    candidates = scan_market(stocks)
    
    if not candidates:
        log("⚠️ 未筛选出候选股票")
    
    # 第五步：生成报告
    report = generate_report(candidates, market_summary, source_name)
    
    # 第六步：保存
    os.makedirs('reports', exist_ok=True)
    report_file = f"reports/weekly_scan_{datetime.now().strftime('%Y%m%d')}.md"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    log(f"\n📄 报告已保存: {report_file}")
    
    # 第七步：推送
    push_to_pushplus(report)
    
    log("\n✅ 扫描完成")

if __name__ == '__main__':
    main()
