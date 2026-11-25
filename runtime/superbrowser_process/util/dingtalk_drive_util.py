# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable

import requests
from alibabacloud_dingtalk.storage_1_0 import models as storage_models
from alibabacloud_dingtalk.storage_1_0.client import Client as StorageClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from util.dingtalk_doc_api import get_access_token, get_union_id


INVALID_DENTRY_CHARS = r'[\\/:*?"<>|]'


@dataclass
class DingDriveDentry:
    id: str
    name: str
    type: str = ""
    parent_id: str = ""
    space_id: str = ""


def sanitize_dentry_name(name: str) -> str:
    cleaned = re.sub(INVALID_DENTRY_CHARS, "_", str(name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError("钉盘文件或目录名称不能为空")
    return cleaned[:240]


def calc_md5(file_path: str) -> str:
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def create_client() -> StorageClient:
    config = open_api_models.Config(protocol="https", region_id="central")
    return StorageClient(config)


def _dentry_from_model(model) -> DingDriveDentry:
    return DingDriveDentry(
        id=str(getattr(model, "id", "") or ""),
        name=str(getattr(model, "name", "") or ""),
        type=str(getattr(model, "type", "") or ""),
        parent_id=str(getattr(model, "parent_id", "") or ""),
        space_id=str(getattr(model, "space_id", "") or ""),
    )


def _is_folder_type(type_name: str) -> bool:
    return str(type_name or "").lower() in {"folder", "directory", "dir"}


def _is_file_type(type_name: str) -> bool:
    return str(type_name or "").lower() in {"file"}


class DingDriveClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        user_id: str,
        space_id: str,
        root_parent_id: str = "0",
    ):
        if not all([app_key, app_secret, user_id, space_id]):
            raise ValueError("钉盘配置不完整，请检查 app_key、app_secret、user_id、space_id")

        self.app_key = app_key
        self.app_secret = app_secret
        self.user_id = user_id
        self.space_id = str(space_id)
        self.root_parent_id = str(root_parent_id or "0")
        self.client = create_client()
        self.access_token = get_access_token(app_key, app_secret)
        self.union_id = get_union_id(self.access_token, user_id)

    def _headers(self, headers_cls):
        return headers_cls(x_acs_dingtalk_access_token=self.access_token)

    def list_children(self, parent_id: str) -> list[DingDriveDentry]:
        dentries: list[DingDriveDentry] = []
        next_token = None
        while True:
            headers = self._headers(storage_models.ListDentriesHeaders)
            request = storage_models.ListDentriesRequest(
                parent_id=str(parent_id),
                union_id=self.union_id,
                max_results=50,
                next_token=next_token,
                with_thumbnail=False,
            )
            response = self.client.list_dentries_with_options(
                self.space_id,
                request,
                headers,
                util_models.RuntimeOptions(),
            )
            body = response.body
            for item in getattr(body, "dentries", None) or []:
                dentries.append(_dentry_from_model(item))
            next_token = getattr(body, "next_token", None)
            if not next_token:
                break
        return dentries

    def find_child(
        self,
        parent_id: str,
        name: str,
        expected_type: str | Iterable[str] | None = None,
    ) -> DingDriveDentry | None:
        target_name = str(name or "").strip()
        expected_types = None
        if isinstance(expected_type, str):
            expected_types = {expected_type.lower()}
        elif expected_type:
            expected_types = {str(item).lower() for item in expected_type}

        for child in self.list_children(parent_id):
            if child.name != target_name:
                continue
            if expected_types and child.type.lower() not in expected_types:
                continue
            return child
        return None

    def ensure_folder(self, parent_id: str, folder_name: str) -> DingDriveDentry:
        folder_name = sanitize_dentry_name(folder_name)
        existing = self.find_child(parent_id, folder_name)
        if existing and _is_folder_type(existing.type):
            return existing

        headers = self._headers(storage_models.AddFolderHeaders)
        request = storage_models.AddFolderRequest(
            name=folder_name,
            union_id=self.union_id,
            option=None,
        )
        try:
            response = self.client.add_folder_with_options(
                self.space_id,
                str(parent_id),
                request,
                headers,
                util_models.RuntimeOptions(),
            )
            dentry = getattr(response.body, "dentry", None)
            if dentry:
                created = _dentry_from_model(dentry)
                if created.id:
                    return created
        except Exception:
            # Race condition or older API conflict-strategy support: re-check before surfacing.
            relisted = self.find_child(parent_id, folder_name)
            if relisted and _is_folder_type(relisted.type):
                return relisted
            raise

        relisted = self.find_child(parent_id, folder_name)
        if relisted and _is_folder_type(relisted.type):
            return relisted
        raise RuntimeError(f"创建钉盘目录失败：{folder_name}")

    def ensure_path(self, path_parts: Iterable[str], parent_id: str | None = None) -> DingDriveDentry:
        current = DingDriveDentry(
            id=str(parent_id or self.root_parent_id),
            name="",
            type="folder",
            space_id=self.space_id,
        )
        for part in path_parts:
            current = self.ensure_folder(current.id, part)
        return current

    def _get_upload_info(self, file_path: str, parent_id: str, file_name: str):
        file_size = os.path.getsize(file_path)
        file_md5 = calc_md5(file_path)
        headers = self._headers(storage_models.GetFileUploadInfoHeaders)
        pre_check = storage_models.GetFileUploadInfoRequestOptionPreCheckParam(
            md_5=file_md5,
            size=file_size,
            parent_id=str(parent_id),
            name=file_name,
        )
        option = storage_models.GetFileUploadInfoRequestOption(
            storage_driver="DINGTALK",
            pre_check_param=pre_check,
            prefer_region="ZHANGJIAKOU",
            prefer_intranet=False,
        )
        request = storage_models.GetFileUploadInfoRequest(
            union_id=self.union_id,
            protocol="HEADER_SIGNATURE",
            multipart=False,
            option=option,
        )
        return self.client.get_file_upload_info_with_options(
            self.space_id,
            request,
            headers,
            util_models.RuntimeOptions(),
        ).body

    @staticmethod
    def _upload_resource(resource_url: str, headers: dict, file_path: str) -> None:
        with open(file_path, "rb") as f:
            response = requests.put(resource_url, headers=headers or {}, data=f, timeout=90)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"钉盘资源上传失败：HTTP {response.status_code} {response.text[:300]}")

    def _commit_file(
        self,
        upload_key: str,
        file_name: str,
        parent_id: str,
        file_size: int,
        overwrite_dentry_id: str | None = None,
    ) -> DingDriveDentry:
        headers = self._headers(storage_models.CommitFileHeaders)
        option = storage_models.CommitFileRequestOption(
            size=file_size,
            conflict_strategy="OVERWRITE" if overwrite_dentry_id else "AUTO_RENAME",
            app_properties=[],
        )
        request = storage_models.CommitFileRequest(
            upload_key=upload_key,
            name=file_name,
            parent_id=str(parent_id),
            overwrite_dentry_id=overwrite_dentry_id,
            option=option,
            union_id=self.union_id,
        )
        response = self.client.commit_file_with_options(
            self.space_id,
            request,
            headers,
            util_models.RuntimeOptions(),
        )
        dentry = getattr(response.body, "dentry", None)
        if not dentry:
            raise RuntimeError(f"钉盘文件提交失败：{file_name}")
        return _dentry_from_model(dentry)

    def upload_file(
        self,
        file_path: str,
        parent_id: str,
        file_name: str | None = None,
        overwrite: bool = True,
    ) -> DingDriveDentry:
        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)

        final_name = sanitize_dentry_name(file_name or os.path.basename(file_path))
        existing = self.find_child(parent_id, final_name) if overwrite else None
        if existing and not _is_file_type(existing.type):
            raise RuntimeError(f"钉盘中已存在同名目录，无法覆盖为文件：{final_name}")

        upload_info = self._get_upload_info(file_path, parent_id, final_name)
        signature_info = getattr(upload_info, "header_signature_info", None)
        resource_urls = getattr(signature_info, "resource_urls", None) or []
        upload_headers = getattr(signature_info, "headers", None) or {}
        upload_key = str(getattr(upload_info, "upload_key", "") or "")
        if not resource_urls or not upload_key:
            raise RuntimeError(f"获取钉盘上传地址失败：{final_name}")

        self._upload_resource(resource_urls[0], upload_headers, file_path)
        return self._commit_file(
            upload_key=upload_key,
            file_name=final_name,
            parent_id=parent_id,
            file_size=os.path.getsize(file_path),
            overwrite_dentry_id=existing.id if existing else None,
        )
