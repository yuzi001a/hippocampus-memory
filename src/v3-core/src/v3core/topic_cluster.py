"""冷启动主题聚类 — 扫历史对话 → 向量聚类 → LLM 命名

用法:
    python -m v3core.topic_cluster [--dry-run]
"""
import argparse, json, os, sys, re, hashlib, time
from datetime import datetime
from pathlib import Path

# 确保能找到 v3core

try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from v3core.topic_store import TopicStore
from v3core.config import _resolve_data_dir

try:
    import numpy as np
except ImportError:
    np = None
    print("⚠️ numpy 未安装，请 pip install numpy")

import requests

# ─── 读取历史对话 ───

def parse_turns(content: str) -> dict:
    """把 turns.md 解析成结构化数据"""
    import re
    result = {
        'qa_pairs': [],
        'turn_count': 0,
        'has_tool_calls': False,
        'first_time': '',
        'last_time': '',
        'session_title': ''
    }
    
    # 解析每个 turn
    turns = re.split(r'## Turn \d+\n', content)
    
    current_question = ''
    current_answer = ''
    current_tools = []
    
    for turn in turns[1:]:  # skip header before first turn
        lines = turn.strip().split('\n')
        if not lines:
            continue
        
        # 提取时间戳
        ts_match = re.search(r'timestamp:\s*([\d\-T:\.]+)', lines[0])
        ts = ts_match.group(1) if ts_match else ''
        role_match = re.search(r'role:\s*(\w+)', lines[0])
        role = role_match.group(1) if role_match else ''
        
        # 提取正文
        body = '\n'.join(l for l in lines[1:] if not l.startswith('>') and l.strip())
        body = re.sub(r'\*\*(user|assistant):\*\*', '', body).strip()
        body = re.sub(r'^---$', '', body).strip()
        
        if not body:
            continue
        
        if role == 'user':
            if current_question and current_answer:
                result['qa_pairs'].append({
                    'user': current_question[:500],
                    'assistant': current_answer[:800],
                    'tools': current_tools[:3]
                })
                current_tools = []
            current_question = body
            current_answer = ''
        elif role == 'assistant':
            if current_answer:
                current_answer += '\n' + body[:200]
            else:
                current_answer = body[:800]
            # 检测工具调用
            if 'tool_call' in body.lower() or 'tool' in body.lower()[:50]:
                current_tools.append(body[:100])
                result['has_tool_calls'] = True
        
        if not result['first_time']:
            result['first_time'] = ts
        result['last_time'] = ts
    
    # 最后一对
    if current_question and current_answer:
        result['qa_pairs'].append({
            'user': current_question[:500],
            'assistant': current_answer[:800],
            'tools': current_tools[:3]
        })
    
    result['turn_count'] = len(turns) - 1
    return result


def scan_j_dir(j_path: str, max_files: int = 500) -> list[dict]:
    """扫 j/ 目录，结构化解析对话文件"""
    results = []
    j_path = Path(j_path)
    
    for entry in sorted(j_path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        for f in entry.iterdir():
            if not f.is_file() or f.suffix in ('.py', '.bak'):
                continue
            try:
                text = f.read_text(encoding='utf-8', errors='replace')
                structured = parse_turns(text)
                if not structured['qa_pairs']:
                    continue
                
                entry_data = {
                    'file': str(f),
                    'size': len(text),
                    'content': text[:2000],  # 保留原始文本用于 embedding
                    'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    'turn_count': structured['turn_count'],
                    'qa_pairs': structured['qa_pairs'][:5],  # 最多 5 轮 QA
                    'has_tools': structured['has_tool_calls'],
                    'first_time': structured['first_time'],
                    'last_time': structured['last_time'],
                }
                
                # 从 QA 中提取简短标题（第一轮用户问题截取）
                if structured['qa_pairs']:
                    first_q = structured['qa_pairs'][0]['user']
                    entry_data['title_hint'] = first_q[:40]
                
                results.append(entry_data)
            except Exception as e:
                pass
        if len(results) >= max_files:
            break
    
    return results


def scan_db_sessions(store, limit: int = 200) -> list[dict]:
    """扫 Hermes session DB 获取最近对话"""
    results = []
    db_path = os.path.expanduser('~/AppData/Local/hermes/sessions.db')
    if not os.path.exists(db_path):
        return results
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        # 取最近 session 的消息
        rows = conn.execute("""
            SELECT s.id, s.title, m.role, m.content, m.created_at
            FROM messages m JOIN sessions s ON m.session_id = s.id
            WHERE m.content IS NOT NULL AND length(m.content) > 50
            ORDER BY m.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        # 按 session 分组
        sessions = {}
        for row in rows:
            sid = row[0]
            if sid not in sessions:
                sessions[sid] = {'title': row[1], 'messages': []}
            sessions[sid]['messages'].append({
                'role': row[2], 'content': row[3][:1000], 'time': row[4]
            })
        for sid, s in sessions.items():
            content = '\n'.join(
                f"{m['role']}: {m['content'][:500]}" for m in s['messages'][:5]
            )
            if len(content) > 80:
                results.append({
                    'file': f"session:{sid}",
                    'size': len(content),
                    'content': content[:2000],
                    'modified': s['messages'][0]['time'] if s['messages'] else ''
                })
    except Exception as e:
        print(f"  读 session DB 失败: {_safe_err(e)}")
    return results


# ─── Embedding ───

def compute_embedding(text: str, embed_cfg: dict) -> list[float]:
    from v3core.embedding import call_embedding
    return call_embedding(text[:2000], embed_cfg, cache=True)


# ─── 聚类 ───

def cluster_embeddings(embeddings: list, labels: list, min_cluster_size: int = 3):
    """HDBSCAN 聚类。如果不可用，回退到简单相似度分组"""
    if np is None:
        return _simple_cluster(embeddings, labels)
    try:
        import hdbscan
        X = np.array(embeddings)
        clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, 
                                     min_samples=1, metric='euclidean')
        cluster_labels = clusterer.fit_predict(X)
        groups = {}
        for i, lbl in enumerate(cluster_labels):
            lbl = int(lbl)
            if lbl not in groups:
                groups[lbl] = []
            groups[lbl].append(labels[i])
        # -1 = 噪声点，归为单独组
        return [{'label': f'cluster_{k}', 'members': v} 
                for k, v in groups.items() if k >= 0]
    except ImportError:
        return _simple_cluster(embeddings, labels)


def _simple_cluster(embeddings: list, labels: list):
    """回退：余弦相似度阈值分组"""
    if np is None:
        return [{'label': 'cluster_0', 'members': labels}]
    X = np.array(embeddings)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    sim = X @ X.T / (norms @ norms.T + 1e-10)
    used = set()
    groups = []
    for i in range(len(labels)):
        if i in used: continue
        cluster = [labels[i]]
        used.add(i)
        for j in range(i+1, len(labels)):
            if j not in used and sim[i][j] > 0.75:
                cluster.append(labels[j])
                used.add(j)
        groups.append({'label': f'cluster_{len(groups)}', 'members': cluster})
    return groups

def _format_for_llm(members: list) -> str:
    """把结构化 QA 数据格式化成 LLM 友好的展示"""
    parts = []
    for m in members[:8]:
        title = m.get('title_hint', '') or ''
        time_range = ''
        if m.get('first_time'):
            time_range = f"{m['first_time'][:10]}"
        
        # 取第一个 QA 对
        qa_text = ''
        qa_pairs = m.get('qa_pairs', [])
        if qa_pairs:
            qa = qa_pairs[0]
            q = qa.get('user', '')[:200]
            a = qa.get('assistant', '')[:300]
            if q:
                qa_text = f"用户问: {q}"
            if a:
                qa_text += f"\n        AI 答: {a}"
        else:
            # 没有 qa_pairs，用原始 content
            content = m.get('content', '') or ''
            if content:
                qa_text = content[:500]
        
        file_short = m.get('file','').split('/')[-2] if '/' in m.get('file','') else m.get('file','')
        turn_info = str(m.get('turn_count',0)) + '轮对话, ' + str(len(qa_pairs)) + '组问答'
        
        parts.append(
            '  [文件] ' + file_short + '\n'
            '  [时间] ' + time_range + '  (' + turn_info + ')\n'
            '  [主题线索] ' + (title or '(无)') + '\n'
            '  [内容] ' + qa_text + '\n'
        )
    
    return '\n'.join(parts)


def name_cluster(cluster_members: list, cluster_idx: int, llm_cfg: dict) -> dict:
    """让 LLM 给一个簇命名、摘要、提取关键词"""
    structured_data = _format_for_llm(cluster_members)
    
    prompt = f"""你是一个主题分析师。以下是一组相关对话记录，它们属于同一个话题。

每条记录包含：对话时间、轮数、主题线索、以及具体的问答内容。

对话记录：
{structured_data}

请分析这些对话的共同主题，然后输出 JSON：
{{
  "topic_name": "简短话题名（10字以内）",
  "summary": "50字以内摘要，说明这个主题是关于什么的",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "category": "项目 | 技术 | 生活 | 思考 | 其他"
}}

注意：
- topic_name 要足够具体，能让人一看就知道是什么
- keywords 覆盖内容涉及的主要概念
- 如果所有记录都围绕一个具体项目/任务，category 填"项目"
"""

    # 调用 LLM
    try:
        endpoint = llm_cfg.get('endpoint', '')
        model = llm_cfg.get('model', '')
        api_key = llm_cfg.get('apiKey', '') or llm_cfg.get('api_key', '')
        proxy = llm_cfg.get('proxy', '')
        
        if not endpoint:
            return _fallback_name(cluster_idx)
        
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        body = {
            "model": model or "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500
        }
        import requests
        # 外部厂商 API 默认不走代理（如 minimaxi.com / siliconflow）；
        # 内网/自建端点按代理走。判断标准：endpoint 域名含已知外部厂商特征。
        headers_for_request = dict(headers)
        if ('minimaxi.com' in endpoint.lower()
                or 'api.siliconflow.cn' in endpoint.lower()):
            resp = requests.post(endpoint.rstrip('/') + '/chat/completions',
                                 json=body, headers=headers_for_request, timeout=60)
        else:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.post(endpoint.rstrip('/') + '/chat/completions',
                                 json=body, headers=headers_for_request, proxies=proxies, timeout=60)
        resp.raise_for_status()
        reply = resp.json()['choices'][0]['message']['content']
        
        # 解析 JSON — 兼容各种格式
        reply = reply.strip()
        # 去掉 markdown 代码块
        if reply.startswith('```'):
            # 找第一个换行，去掉开头的 ```xxx
            first_nl = reply.find('\n')
            if first_nl > 0:
                reply = reply[first_nl:]
            # 去掉结尾的 ```
            if reply.endswith('```'):
                reply = reply[:-3]
            reply = reply.strip()
        # 找第一个 { 和最后一个 }
        start = reply.find('{')
        end = reply.rfind('}')
        if start >= 0 and end > start:
            reply = reply[start:end+1]
        if not reply:
            raise ValueError("empty JSON after extraction")
        result = json.loads(reply)
        return result
    except Exception as e:
        print(f"  LLM 命名失败: {_safe_err(e)}")
        return _fallback_name(cluster_idx)


def _fallback_name(cluster_idx: int) -> dict:
    return {
        "topic_name": f"主题_{cluster_idx}",
        "summary": "待人工命名",
        "keywords": ["待定"],
        "category": "其他"
    }


# ─── 整合：聚合并写入 topic_store ───

def run_cold_start(store: TopicStore, embed_cfg: dict, llm_cfg: dict,
                   dry_run: bool = False, max_files: int = 300):
    print("=" * 60)
    print(f"v3 冷启动主题聚类")
    print(f"dry_run = {dry_run}, max_files = {max_files}")
    print("=" * 60)
    
    # 1. 扫描历史对话
    print("\n[1/4] 扫描历史对话...")
    # v3 数据目录（走 config.base_path）
    v3_data_dir = str(_resolve_data_dir())
    j_path = os.path.join(v3_data_dir, 'j')
    if not os.path.exists(j_path):
        j_path = os.getenv('V3_J_DIR', '') or j_path
    print(f"  搜索路径: {j_path}")
    
    docs = scan_j_dir(j_path, max_files)
    print(f"  从 j/ 找到 {len(docs)} 个文件")
    
    if len(docs) < 10:
        print("  j/ 文件不足，从 session DB 补...")
        docs2 = scan_db_sessions(store, limit=max_files)
        print(f"  从 session DB 找到 {len(docs2)} 条")
        docs.extend(docs2)
    
    print(f"  共 {len(docs)} 条对话记录")
    
    if not docs:
        print("❌ 没有找到历史对话，无法聚类")
        return
    
    # 2. 计算 embedding
    print(f"\n[2/4] 计算 embedding...")
    embeddings = []
    valid_docs = []
    for i, doc in enumerate(docs):
        try:
            emb = compute_embedding(doc['content'], embed_cfg)
            if emb and len(emb) > 0:
                embeddings.append(emb)
                valid_docs.append(doc)
            if (i+1) % 20 == 0:
                print(f"  {i+1}/{len(docs)}...")
        except Exception as e:
            pass
    
    print(f"  成功计算 {len(embeddings)}/{len(docs)} 个 embedding")
    
    if len(embeddings) < 5:
        print("❌ embedding 太少，无法聚类")
        return
    
    # 3. 聚类
    print(f"\n[3/4] 聚类...")
    groups = cluster_embeddings(embeddings, valid_docs)
    print(f"  生成 {len(groups)} 个主题簇")
    for g in groups:
        print(f"  {g['label']}: {len(g['members'])} 条")
    
    if dry_run:
        print(f"\n[dry-run] 跳过 LLM 命名和写入")
        for g in groups:
            print(f"\n--- {g['label']} ({len(g['members'])} 条) ---")
            for m in g['members'][:3]:
                print(f"  [{m.get('modified','')[:10]}] {m['content'][:100]}...")
        return
    
    # 4. LLM 命名并写入
    print(f"\n[4/4] LLM 命名主题并写入 topic_store...")
    created = 0
    topic_cluster_map = {}  # topic_id -> members
    
    for idx, g in enumerate(groups):
        print(f"\n  🏷️  命名 {g['label']}...")
        result = name_cluster(g['members'], idx, llm_cfg)
        topic_name = result.get('topic_name', f'主题_{idx}')
        summary = result.get('summary', '')
        keywords = result.get('keywords', [])
        category = result.get('category', '其他')
        
        print(f"    名称: {topic_name}")
        print(f"    摘要: {summary}")
        print(f"    关键词: {keywords}")
        
        tid = store.upsert_topic(
            title=topic_name,
            summary=summary,
            body='\n'.join(m['content'][:500] for m in g['members'][:3]),
            keywords=keywords,
            category=category
        )
        topic_cluster_map[tid] = g['members']
        created += 1
    
    # 写条目
    for tid, members in topic_cluster_map.items():
        for m in members:
            store.add_entry(
                topic_id=tid,
                source='user',
                question=m['content'][:200],
                message_id=m['file']
            )
    
    print(f"\n✅ 完成！创建了 {created} 个主题")
    print(f"📊 主题库统计: {store.stats()}")
    return topic_cluster_map


def _to_dict(obj):
    """V3Config 对象转 dict"""
    if hasattr(obj, 'to_legacy_dict'):
        return obj.to_legacy_dict()
    if hasattr(obj, '__dict__'):
        return {k:v for k,v in obj.__dict__.items() if not k.startswith('_')}
    return {}


def secondary_cluster(labels: list, pairs: list, named: dict,
                     threshold: float = 0.65, llm_cfg: dict = None) -> dict:
    """二次聚簇：合并相似簇、重命名 fallback（标准后处理流程）
    
    实验验证（2026-07-07）：累积文本匹配已有主题，前 5-9 轮可收敛到正确或相近主题。
    这个步骤不应该只存在于记录里——它是新版聚类管线的标配后处理。
    """
    import json, time
    from collections import Counter
    
    # 1. 对 fallback 簇（主题_XX）直接用 LLM 重命名
    fallbacks = {k: v for k, v in named.items() if v.get('title', '').startswith('主题_')}
    if fallbacks and llm_cfg:
        for k, v in fallbacks.items():
            idxs = [i for i, l in enumerate(labels) if str(l) == k]
            if not idxs:
                continue
            lines = []
            for idx in idxs[:3]:
                q = pairs[idx].get('query', '')[:200].replace('\n', ' ').strip()
                a = pairs[idx].get('response', '')[:300].replace('\n', ' ').strip()
                if q and a:
                    lines.append(q)
            text = '\n'.join(lines)
            if not text:
                continue
            prompt = '用8字内概括主题名（只说名称）：\n' + text
            try:
                headers = {'Authorization': f'Bearer {llm_cfg.get("api_key", "")}',
                           'Content-Type': 'application/json'}
                body = {'model': llm_cfg.get('model', ''),
                        'messages': [{'role': 'user', 'content': prompt}],
                        'temperature': 0.1, 'max_tokens': 20}
                r = requests.post(f"{llm_cfg.get('endpoint', '')}/chat/completions",
                                   json=body, headers=headers, timeout=30)
                if r.status_code == 200:
                    reply = r.json()['choices'][0]['message']['content']
                    clean = reply.split('：')[-1].strip().replace('<think>', '').split('\n')[0][:15]
                    if clean:
                        named[k]['title'] = clean
            except Exception:
                pass
            time.sleep(0.5)
    
    # 2. 同类合并：检查名称明显相似的主题
    titles = {k: v['title'] for k, v in named.items()}
    # 按关键词分组
    from collections import defaultdict
    by_kw = defaultdict(list)
    for k, t in titles.items():
        # 取中文 2-gram 作为特征
        grams = set()
        for i in range(0, len(t) - 1):
            if '\u4e00' <= t[i] <= '\u9fff' and '\u4e00' <= t[i+1] <= '\u9fff':
                grams.add(t[i:i+2])
        for g in grams:
            by_kw[g].append(k)
    
    # 合并共享 2 个及以上 2-gram 的簇
    merged = set()
    for g, ks in by_kw.items():
        if len(ks) < 2:
            continue
        for i in range(len(ks)):
            for j in range(i+1, len(ks)):
                k1, k2 = ks[i], ks[j]
                if k1 == k2 or k1 in merged or k2 in merged:
                    continue
                t1, t2 = titles[k1], titles[k2]
                # 检查重叠字
                common = sum(1 for c in set(t1) if c in set(t2) and '\u4e00' <= c <= '\u9fff')
                if common >= 2 and len(t1) <= 12 and len(t2) <= 12:
                    merged.add(min(k1, k2, key=lambda x: len(titles[x])))
    
    if merged:
        for k in sorted(merged):
            named[k]['title'] = titles[k]
    
    return named


def main():
    parser = argparse.ArgumentParser(description="冷启动主题聚类（含二次聚簇后处理）")
    parser.add_argument("--dry-run", action="store_true", help="不写库，仅预览")
    parser.add_argument('--max-files', type=int, default=300, help='最大处理文件数')
    parser.add_argument('--db', type=str, help='topic_store SQLite 路径')
    args = parser.parse_args()
    
    # 加载配置
    from v3core.config import resolve_config
    cfg = resolve_config()
   
    # Embed config — 只接受显式完整配置；不从环境变量补 endpoint/model。
    from v3core.embedding import safe_embed_cfg
    embed_cfg = safe_embed_cfg(cfg)
    if embed_cfg is None:
        print("未配置 embedding，无法运行主题聚类")
        return

    # LLM config — 默认从 cfg.llm 读取（config.yaml 已显式配置），不隐式指向任何厂商
    # 优先从 .env 读 LLM_API_KEY，回退到不同厂商命名变量
    llm_api_key = ''
    env_path = os.path.expanduser('~/AppData/Local/hermes/.env')
    if os.path.exists(env_path):
        for line in open(env_path):
            stripped = line.strip()
            if stripped.startswith('LLM_API_KEY='):
                llm_api_key = stripped.split('=', 1)[1]
                break
            if stripped.startswith('MINIMAX_CN_API_KEY='):
                llm_api_key = stripped.split('=', 1)[1]
                break
    if not llm_api_key:
        llm_api_key = os.environ.get('LLM_API_KEY', '') or os.environ.get('MINIMAX_CN_API_KEY', '')

    # 从配置读取 LLM proxy
    llm_proxy = ''
    if hasattr(cfg, 'llm') and cfg.llm:
        llm_proxy = cfg.llm.proxy if hasattr(cfg.llm, 'proxy') else ''

    llm_cfg = {
        'endpoint': getattr(cfg.llm, 'base_url', '') if hasattr(cfg.llm, 'base_url') else '',
        'model': getattr(cfg.llm, 'model', '') if hasattr(cfg.llm, 'model') else '',
        'api_key': getattr(cfg.llm, 'api_key', '') or llm_api_key,
        'proxy': llm_proxy or os.environ.get('V3CORE_LLM_PROXY', '')
    }
    
    db_path = args.db or str(_resolve_data_dir() / 'v3_topic.db')
    store = TopicStore(db_path)
    
    try:
        run_cold_start(store, embed_cfg, llm_cfg, args.dry_run, args.max_files)
    finally:
        store.close()


if __name__ == '__main__':
    main()
