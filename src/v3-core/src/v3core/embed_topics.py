"""补主题 vector embedding — 摘要 + 关键词 + QA 首条
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from v3core.topic_store import TopicStore
from v3core.embedding import call_embedding, safe_embed_cfg
from v3core.config import resolve_config, _resolve_data_dir
from v3core import _safe_err

def build_topic_text(topic: dict) -> str:
    """把主题信息拼成一段文本用于算向量"""
    parts = []
    
    # 1. 摘要（最核心）
    if topic.get('summary'):
        parts.append(f"主题：{topic['summary']}")
    
    # 2. 关键词（精确锚点）
    keywords = json.loads(topic.get('keywords', '[]'))
    if keywords:
        parts.append(f"关键词：{' '.join(keywords)}")
    
    # 3. 标题
    if topic.get('title'):
        parts.append(topic['title'])
    
    return ' '.join(parts)

def main():
    cfg = resolve_config()
    embed_cfg = safe_embed_cfg(cfg)
    if embed_cfg is None:
        print("未配置 embedding，跳过主题向量回填")
        return

    db_path = str(_resolve_data_dir(cfg) / 'v3_topic.db')
    store = TopicStore(db_path)
    
    # 读取所有主题
    topics = store.get_all_topics()
    print(f"共 {len(topics)} 个主题")
    
    # 统计已有 embedding
    done = store.conn.execute("SELECT COUNT(*) FROM topic_blocks WHERE embedding IS NOT NULL").fetchone()[0]
    print(f"已有 embedding: {done}/{len(topics)}")
    
    success = 0
    fail = 0
    for i, t in enumerate(topics):
        text = build_topic_text(t)
        if not text.strip():
            print(f"  [{i+1}/{len(topics)}] {t['title']}: 无内容，跳过")
            continue
        
        try:
            emb = call_embedding(text, embed_cfg, cache=True)
            store.conn.execute(
                "UPDATE topic_blocks SET embedding=? WHERE id=?",
                (json.dumps(emb), t['id'])
            )
            success += 1
            if (i+1) % 20 == 0:
                store.conn.commit()
                print(f"  [{i+1}/{len(topics)}] 已算 {success} 个")
        except ValueError:
            raise
        except Exception as e:
            print(f"  [{i+1}/{len(topics)}] {t['title']}: 失败 - {_safe_err(e)}")
            fail += 1
    
    store.conn.commit()
    
    # 验证
    done = store.conn.execute("SELECT COUNT(*) FROM topic_blocks WHERE embedding IS NOT NULL").fetchone()[0]
    print(f"\n完成：成功 {success}/{len(topics)}，失败 {fail}")
    print(f"已有 embedding: {done}/{len(topics)}")
    store.close()

if __name__ == '__main__':
    main()
