import os

import pandas as pd

from util import dingtalk_message_util as mg


def clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def safe_sheet_name(value) -> str:
    text = clean_text(value)
    replace_map = {
        "\\": "＼",
        "/": "／",
        "?": "？",
        "*": "＊",
        "[": "【",
        "]": "】",
        ":": "：",
    }
    for old, new in replace_map.items():
        text = text.replace(old, new)
    return (text or "未命名")[:31]


def merge_files_to_one_excel(file_list, output_file):
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        for file_path in file_list:
            df = pd.read_excel(file_path)
            file_name = os.path.splitext(os.path.basename(file_path))[0]
            df.to_excel(writer, sheet_name=safe_sheet_name(file_name), index=False)
    return output_file


def _group_targets(process_config):
    group_file_config = process_config.get("group_file") or {}
    groups = group_file_config.get("groups")
    if isinstance(groups, list) and groups:
        return [group for group in groups if clean_text(group.get("open_conversation_id"))]

    ids = group_file_config.get("open_conversation_ids")
    if ids is None:
        ids = group_file_config.get("open_conversation_id")
    if isinstance(ids, str):
        ids = [ids]

    return [
        {
            "open_conversation_id": clean_text(open_conversation_id),
            "sender_user_id": clean_text(group_file_config.get("sender_user_id")),
            "space_id": clean_text(group_file_config.get("space_id")),
            "parent_id": clean_text(group_file_config.get("parent_id")),
        }
        for open_conversation_id in (ids or [])
        if clean_text(open_conversation_id)
    ]


def send_file_to_group_conversations(file_path, process_config, cfg, log):
    group_file_config = process_config.get("group_file") or {}
    if group_file_config.get("enabled", True) is False:
        log.info("群文件发送未启用，跳过: %s", file_path)
        return

    dingtalk_info = cfg["dingtalk"]["app"]["rpa"]
    targets = _group_targets(process_config)
    if not targets:
        log.info("未配置群文件发送目标，跳过: %s", file_path)
        return

    conflict_strategy = clean_text(group_file_config.get("conflict_strategy")) or "AUTO_RENAME"
    resolve_conversation_space = group_file_config.get("resolve_conversation_space", False)

    for target in targets:
        open_conversation_id = clean_text(target.get("open_conversation_id"))
        result = mg.send_local_file_to_group_conversation(
            str(file_path),
            sender_user_id=clean_text(target.get("sender_user_id")) or clean_text(group_file_config.get("sender_user_id")) or clean_text(dingtalk_info.get("user_id")),
            space_id=clean_text(target.get("space_id")) or clean_text(group_file_config.get("space_id")) or clean_text(dingtalk_info.get("space_id")),
            parent_id=clean_text(target.get("parent_id")) or clean_text(group_file_config.get("parent_id")) or clean_text(dingtalk_info.get("parent_id")),
            open_conversation_id=open_conversation_id,
            conflict_strategy=clean_text(target.get("conflict_strategy")) or conflict_strategy,
            resolve_conversation_space=target.get("resolve_conversation_space", resolve_conversation_space),
        )
        log.info(
            "群文件发送成功: file=%s, open_conversation_id=%s, sender_user_id=%s, space_id=%s, dentry_id=%s",
            file_path,
            open_conversation_id,
            clean_text(target.get("sender_user_id")) or clean_text(group_file_config.get("sender_user_id")) or clean_text(dingtalk_info.get("user_id")),
            result.get("space_id"),
            result.get("dentry_id"),
        )
