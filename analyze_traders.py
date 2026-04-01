import json
import os
import re
from openai import OpenAI

# ============= 配置 =============
API_KEY = os.environ.get('OPENAI_API_KEY', 'sk-6d438420432a406c97bf8abb3328d23f')
BASE_URL = os.environ.get('OPENAI_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
MODEL = os.environ.get('OPENAI_MODEL', 'deepseek-v3.2')

# ============= 第一步：合并所有选手的交易数据 =============
def merge_trader_data():
    """合并所有订单文件到一个文件，并添加用户标识"""
    print("=" * 50)
    print("第一步：合并交易数据...")

    # 读取用户资料
    with open('top10_user_profiles.json', 'r', encoding='utf-8') as f:
        profiles = json.load(f)

    # 创建用户 ID 到用户信息的映射
    user_map = {}
    for p in profiles:
        user_id = p.get('user_id')
        if user_id:
            user_map[str(user_id)] = {
                'rank': p['rank'],
                'nickname': p['nickname'],
                'wallet_address': p['wallet_address'],
                'net_pnl': p['net_pnl'],
                'roi_pct': p['roi_pct'],
                'trade_count': p['trade_count'],
                'win_rate': p['win_rate']
            }

    # 合并所有订单文件
    all_orders = []
    order_files = [f for f in os.listdir('.') if f.startswith('orders_') and f.endswith('.json')]

    for filename in order_files:
        filepath = os.path.join('.', filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            orders = json.load(f)
            for order in orders:
                # 添加用户标识字段
                order['_user_id'] = order.get('user_id')
                order['_user_rank'] = None
                order['_user_nickname'] = None
                order['_user_wallet'] = None

                # 根据 user_id 查找用户信息
                uid = str(order.get('user_id', ''))
                if uid in user_map:
                    order['_user_rank'] = user_map[uid]['rank']
                    order['_user_nickname'] = user_map[uid]['nickname']
                    order['_user_wallet'] = user_map[uid]['wallet_address']

                all_orders.append(order)

    # 按用户 ID 和订单 ID 排序
    all_orders.sort(key=lambda x: (x.get('_user_id', 0), x.get('id', 0)))

    # 写入合并后的文件
    output_file = 'all_traders_combined.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=2)

    # 统计每个用户的订单数
    user_counts = {}
    for order in all_orders:
        nick = order.get('_user_nickname', 'Unknown')
        user_counts[nick] = user_counts.get(nick, 0) + 1

    print(f"  合并完成！总订单数：{len(all_orders)}")
    print(f"  输出文件：{output_file}")
    print("  各用户订单数:")
    for nick, count in sorted(user_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {nick}: {count}单")
    print()

    return all_orders, user_map

# ============= 第二步：按用户分组订单 =============
def group_orders_by_user(all_orders, user_map):
    """按用户分组订单"""
    orders_by_user = {}
    for order in all_orders:
        user_id = order.get('_user_id')
        if user_id not in orders_by_user:
            orders_by_user[user_id] = {
                'info': None,
                'orders': []
            }
        orders_by_user[user_id]['orders'].append(order)

    # 填充用户信息
    for uid, data in orders_by_user.items():
        if str(uid) in user_map:
            data['info'] = {
                'rank': user_map[str(uid)]['rank'],
                'nickname': user_map[str(uid)]['nickname'],
                'user_id': uid
            }

    return orders_by_user

# ============= 第三步：调用 LLM 分析每个选手 =============
def analyze_traders(orders_by_user):
    """调用 LLM 分析每个选手的策略"""
    print("=" * 50)
    print("第二步：调用 LLM 分析选手策略...")

    # 读取提示词模板
    with open('memo.txt', 'r', encoding='utf-8') as f:
        prompt_template = f.read()

    # 初始化客户端
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    # 提取分数的函数
    def extract_score(analysis):
        match = re.search(r'\*\*加权总分\*\*[:：]\s*(\d+(?:\.\d+)?)\s*分', analysis)
        if match:
            return float(match.group(1))
        match = re.search(r'加权总分 [:：]\s*(\d+(?:\.\d+)?)\s*分', analysis)
        if match:
            return float(match.group(1))
        match = re.search(r'(\d+(?:\.\d+)?)\s*分\s*（满分 100）', analysis)
        if match:
            return float(match.group(1))
        match = re.search(r'加权总分.*?\*\*(\d+(?:\.\d+)?)\s*分\*\*', analysis)
        if match:
            return float(match.group(1))
        return 0.0

    # 提取风险等级
    def extract_risk_level(analysis):
        match = re.search(r'\*\*风险等级\*\*[:：]\s*\*\*(.+?)\*\*', analysis)
        if match:
            return match.group(1).strip()
        match = re.search(r'\*\*风险等级\*\*[:：]\s*(\S+)', analysis)
        if match:
            return match.group(1).strip()
        return '未知'

    # 提取关键指标
    def extract_key_metrics(analysis):
        metrics = {}

        # 周期数 - 匹配 "总完整周期数：3" 或 "总完整周期数 3"
        match = re.search(r'总完整周期数\s*[:：]?\s*(\d+)', analysis)
        metrics['cycles'] = match.group(1) if match else 'N/A'

        # 最大加仓层数 - 匹配 "最大加仓层数（最高 level）：2"
        match = re.search(r'最大加仓层数.*?[:：]?\s*(\d+)', analysis)
        metrics['max_level'] = match.group(1) if match else 'N/A'

        # multiplier - 匹配多种格式
        # 格式 1: "最大 multiplier / 平均 multiplier：3 / 2"
        # 格式 2: "最大 multiplier：5 / 平均 multiplier：2.8"
        # 格式 3: "最大 multiplier：最高 4 / 平均 multiplier：2.0"
        match = re.search(r'最大 multiplier\s*[:：]\s*(?:最高\s*)?([\d.]+)\s*/\s*(?:平均\s*)?multiplier\s*[:：]?\s*([\d.]+)', analysis)
        if match:
            metrics['max_mult'] = match.group(1)
            metrics['avg_mult'] = match.group(2)
        else:
            # 尝试简化格式："multiplier：X / Y"
            match = re.search(r'multiplier\s*[:：]\s*([\d.]+)\s*/\s*([\d.]+)', analysis)
            if match:
                metrics['max_mult'] = match.group(1)
                metrics['avg_mult'] = match.group(2)
            else:
                metrics['max_mult'] = 'N/A'
                metrics['avg_mult'] = 'N/A'

        # 提取 strategy_id - 优先匹配 [UUID1,UUID2] 格式
        match = re.search(r'strategy_id[:：\s]*\[([^\]]+)\]', analysis)
        if match:
            strategy_content = match.group(1).strip()
            if strategy_content.upper() == 'N/A':
                metrics['strategy_id'] = 'N/A'
            else:
                # 提取 UUID 列表
                uuids = [s.strip() for s in strategy_content.split(',')]
                metrics['strategy_id'] = ','.join(uuids)
        else:
            # 退而求其次，在全文中查找所有完整 UUID
            uuid_pattern = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
            uuids = uuid_pattern.findall(analysis)

            if uuids:
                # 去重（保留顺序）
                seen = set()
                unique_uuids = []
                for u in uuids:
                    if u.lower() not in seen:
                        seen.add(u.lower())
                        unique_uuids.append(u)
                metrics['strategy_id'] = ','.join(unique_uuids)
            else:
                metrics['strategy_id'] = 'N/A'

        # 提取主要交易品种
        match = re.search(r'主要交易品种\s*[:：]\s*(\w+)', analysis)
        if match:
            metrics['main_symbol'] = match.group(1).strip()
        else:
            metrics['main_symbol'] = 'N/A'

        return metrics

    results = []

    for user_id, data in sorted(orders_by_user.items(), key=lambda x: x[1]['info'].get('rank', 99) if x[1]['info'] else 99):
        info = data['info']
        orders = data['orders']

        if not orders:
            print(f"  跳过 {info.get('nickname') if info else user_id}: 无订单数据")
            continue

        nickname = info.get('nickname', 'Unknown') if info else 'Unknown'
        orig_rank = info.get('rank', 'N/A') if info else 'N/A'

        print(f"  正在分析原排名第{orig_rank}的选手：{nickname} (共{len(orders)}单)...")

        # 构建提示词 - 限制 JSON 大小
        orders_json_encoded = json.dumps(orders[:100], ensure_ascii=False, indent=2) if len(orders) > 100 else json.dumps(orders, ensure_ascii=False, indent=2)
        prompt = prompt_template.replace('%s', orders_json_encoded)

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": "你是一位专业的量化交易策略分析专家"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7
            )

            analysis = response.choices[0].message.content
            score = extract_score(analysis)
            risk_level = extract_risk_level(analysis)
            metrics = extract_key_metrics(analysis)

            results.append({
                'orig_rank': orig_rank,
                'nickname': nickname,
                'user_id': user_id,
                'score': score,
                'risk_level': risk_level,
                'metrics': metrics,
                'analysis': analysis
            })

            print(f"    分析完成！加权总分：{score}，风险等级：{risk_level}")

        except Exception as e:
            print(f"    分析失败：{e}")
            results.append({
                'orig_rank': orig_rank,
                'nickname': nickname,
                'user_id': user_id,
                'score': 0.0,
                'risk_level': '分析失败',
                'metrics': {},
                'analysis': f"分析出错：{str(e)}"
            })

    return results

# ============= 第四步：生成报告 =============
def generate_report(results):
    """生成排名报告"""
    print()
    print("=" * 50)
    print("第三步：生成排名报告...")

    # 按加权总分倒序排序
    results_sorted = sorted(results, key=lambda x: x['score'], reverse=True)

    # 排名依据说明
    ranking_rationale = """# Top10 参赛选手策略分析报告

## 排名依据说明

本次排名基于 **8 个维度加权评分**，按加权总分从高到低排序：

| 维度 | 权重 | 评分要点 |
|------|------|----------|
| 1. 爆仓风险/极端回撤控制 | 30% | 最大加仓层数、multiplier 是否≥4、是否≥8 层高危 |
| 2. 资金曲线健康度与稳定性 | 20% | 是否多次深跌后暴拉、止盈是否及时回收成本 |
| 3. 加仓激进度与参数合理性 | 15% | multiplier 固定/递增、平均值与最大值 |
| 4. 止盈/回收效率 | 15% | take_profit/manual_close 是否成功回收全部成本 |
| 5. 样本量与行情覆盖 | 10% | 完整马丁周期数量、是否覆盖多种行情 |
| 6. 风险控制措施 | 5% | 多品种分散、层数上限、回撤保护等安全阀 |
| 7. 策略一致性与可复制性 | 3% | 参数逻辑是否固定、可复制 |
| 8. 整体可微调空间 | 2% | 参数优化空间是否明确 |

**加权总分 = 各维度得分 × 对应权重之和（满分 100 分）**

### 风险等级划分
- **相对安全**：得分≥80，风险参数保守，资金曲线平稳
- **中等可控**：得分 70-79，风险参数适中，有一定风控措施
- **中高风险**：得分 40-69，激进参数明显，风控措施不足
- **高危**：得分<40，高危参数（multiplier≥4 或层数≥8），缺乏硬性风控

---

"""

    # 生成报告
    report = ranking_rationale
    report += "## 最终排名总览\n\n"
    report += "| 排名 | 选手昵称 | 风险等级 | 周期数 | 最大层数 | 最大 multiplier |\n"
    report += "|------|----------|----------|--------|----------|----------------|\n"

    for i, r in enumerate(results_sorted, 1):
        m = r.get('metrics', {})
        report += f"| {i} | {r['nickname']} | {r['risk_level']} | {m.get('cycles', 'N/A')} | {m.get('max_level', 'N/A')} | {m.get('max_mult', 'N/A')} |\n"

    report += "\n---\n\n"

    for i, r in enumerate(results_sorted, 1):
        report += f"## 第{i}名：{r['nickname']}（{r['risk_level']}）\n\n"
        report += f"{r['analysis']}\n\n"
        report += "---\n\n"

    with open('trader_analysis_report.md', 'w', encoding='utf-8') as f:
        f.write(report)

    # 保存 JSON（移除 analysis 和 score 字段，risk_level 放在第 2 位）
    results_output = []
    for i, r in enumerate(results_sorted, 1):
        m = r.get('metrics', {})
        results_output.append({
            'rank': i,
            'risk_level': r['risk_level'],
            'nickname': r['nickname'],
            'user_id': r['user_id'],
            'strategy_id': m.get('strategy_id', 'N/A') if m.get('strategy_id') else 'N/A',
            'metrics': {
                'cycles': m.get('cycles', 'N/A') if m.get('cycles') else 'N/A',
                'max_level': m.get('max_level', 'N/A') if m.get('max_level') else 'N/A',
                'max_mult': m.get('max_mult', 'N/A') if m.get('max_mult') else 'N/A',
                'avg_mult': m.get('avg_mult', 'N/A') if m.get('avg_mult') else 'N/A',
                'main_symbol': m.get('main_symbol', 'N/A') if m.get('main_symbol') else 'N/A'
            }
        })

    with open('trader_analysis_results.json', 'w', encoding='utf-8') as f:
        json.dump(results_output, f, ensure_ascii=False, indent=2)

    # 打印最终排名
    print()
    print("=" * 50)
    print("最终排名（按加权总分倒序）:")
    print()
    print("| 排名 | 选手昵称 | 加权总分 | 风险等级 |")
    print("|------|----------|----------|----------|")
    for i, r in enumerate(results_sorted, 1):
        print(f"| {i} | {r['nickname']} | {r['score']:.1f} | {r['risk_level']} |")

    print()
    print("输出文件:")
    print("  - all_traders_combined.json (合并后的交易数据)")
    print("  - trader_analysis_report.md (Markdown 分析报告)")
    print("  - trader_analysis_results.json (JSON 格式结果)")

# ============= 主程序 =============
if __name__ == '__main__':
    # 第一步：合并数据
    all_orders, user_map = merge_trader_data()

    # 第二步：按用户分组
    orders_by_user = group_orders_by_user(all_orders, user_map)

    # 第三步：调用 LLM 分析
    results = analyze_traders(orders_by_user)

    # 第四步：生成报告
    generate_report(results)

    print()
    print("=" * 50)
    print("全部完成！")
