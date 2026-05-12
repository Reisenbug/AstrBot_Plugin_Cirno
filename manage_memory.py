#!/usr/bin/env python3
"""
琪露诺记忆管理工具
用法: python3 manage_memory.py
"""
import json
import sqlite3
import sys
from datetime import datetime

DB = "/Users/lhy/Documents/AstrBot/data/data_v4.db"


def load(key):
    db = sqlite3.connect(DB)
    row = db.execute("SELECT value FROM preferences WHERE key=?", [key]).fetchone()
    db.close()
    if not row:
        return []
    data = json.loads(row[0])
    return data.get("val", data) if isinstance(data, dict) else data


def save(key, val):
    db = sqlite3.connect(DB)
    db.execute("UPDATE preferences SET value=? WHERE key=?",
               [json.dumps({"val": val}, ensure_ascii=False), key])
    db.commit()
    db.close()


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def list_entries(entries, label):
    print(f"\n【{label}】共 {len(entries)} 条")
    for i, e in enumerate(entries):
        ts = fmt_ts(e.get("ts", 0))
        text = e.get("text", "")
        has_vec = "✓" if e.get("vec") else "✗"
        print(f"  [{i}] {ts} vec={has_vec}  {text[:60]}{'…' if len(text)>60 else ''}")


def menu_entries(entries, key, label):
    while True:
        list_entries(entries, label)
        print("\n  d <序号>   删除")
        print("  e <序号>   编辑文本")
        print("  q          返回")
        cmd = input("> ").strip()
        if cmd == "q":
            break
        parts = cmd.split(None, 1)
        if len(parts) != 2:
            continue
        action, idx_str = parts
        try:
            idx = int(idx_str)
            assert 0 <= idx < len(entries)
        except Exception:
            print("无效序号")
            continue
        if action == "d":
            removed = entries.pop(idx)
            save(key, entries)
            print(f"已删除: {removed.get('text','')[:60]}")
        elif action == "e":
            old = entries[idx].get("text", "")
            print(f"当前: {old}")
            new = input("新内容 (回车取消): ").strip()
            if new:
                entries[idx]["text"] = new
                entries[idx].pop("vec", None)
                save(key, entries)
                print("已更新（vec 已清除，下次压缩时重新生成）")


def menu_core():
    db = sqlite3.connect(DB)
    row = db.execute("SELECT value FROM preferences WHERE key='core_memory'").fetchone()
    db.close()
    if not row:
        print("无核心记忆数据")
        return
    data = json.loads(row[0])
    profiles = data.get("val", data) if isinstance(data, dict) else data

    while True:
        print("\n【核心记忆】")
        uids = list(profiles.keys())
        for i, uid in enumerate(uids):
            p = profiles[uid]
            print(f"  [{i}] {p.get('name', uid)}({uid})  {p.get('relationship','')[:40]}")
        print("\n  <序号>   查看/编辑某人")
        print("  q        返回")
        cmd = input("> ").strip()
        if cmd == "q":
            break
        try:
            idx = int(cmd)
            uid = uids[idx]
        except Exception:
            continue
        menu_profile(profiles, uid, data, "core_memory")


def menu_profile(profiles, uid, raw_data, key):
    p = profiles[uid]
    while True:
        print(f"\n【{p.get('name', uid)}】")
        print(f"  relationship : {p.get('relationship','')}")
        print(f"  traits       : {p.get('traits', [])}")
        print(f"  events       : {p.get('important_events', [])}")
        print("\n  r   编辑 relationship")
        print("  t   编辑 traits（逗号分隔）")
        print("  de <序号>  删除某条 event")
        print("  ae <内容>  添加 event")
        print("  q   返回")
        cmd = input("> ").strip()
        if cmd == "q":
            break
        elif cmd == "r":
            new = input(f"新 relationship (回车取消): ").strip()
            if new:
                p["relationship"] = new
                _save_core(profiles, raw_data, key)
                print("已更新")
        elif cmd == "t":
            new = input("新 traits（逗号分隔，回车取消）: ").strip()
            if new:
                p["traits"] = [t.strip() for t in new.split(",") if t.strip()][:5]
                _save_core(profiles, raw_data, key)
                print("已更新")
        elif cmd.startswith("de "):
            try:
                i = int(cmd[3:])
                removed = p["important_events"].pop(i)
                _save_core(profiles, raw_data, key)
                print(f"已删除: {removed}")
            except Exception:
                print("无效序号")
        elif cmd.startswith("ae "):
            text = cmd[3:].strip()[:50]
            if text:
                p.setdefault("important_events", []).append(text)
                if len(p["important_events"]) > 3:
                    p["important_events"].pop(0)
                _save_core(profiles, raw_data, key)
                print("已添加")


def _save_core(profiles, raw_data, key):
    if isinstance(raw_data, dict) and "val" in raw_data:
        raw_data["val"] = profiles
        val = raw_data
    else:
        val = profiles
    db = sqlite3.connect(DB)
    db.execute("UPDATE preferences SET value=? WHERE key=?",
               [json.dumps(val, ensure_ascii=False), key])
    db.commit()
    db.close()


def main():
    while True:
        print("\n=== 琪露诺记忆管理 ===")
        print("  1  L1 摘要 (recall_summaries)")
        print("  2  L2 浓缩 (recall_digests)")
        print("  3  核心记忆 (core_memory)")
        print("  q  退出")
        cmd = input("> ").strip()
        if cmd == "q":
            break
        elif cmd == "1":
            entries = load("recall_summaries")
            menu_entries(entries, "recall_summaries", "L1 摘要")
        elif cmd == "2":
            entries = load("recall_digests")
            menu_entries(entries, "recall_digests", "L2 浓缩")
        elif cmd == "3":
            menu_core()


if __name__ == "__main__":
    main()
