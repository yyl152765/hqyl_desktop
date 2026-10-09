"""Mabang TEMU channel settings, based on the platform-logistics page contract.

Only enabled (ON) rows exposing the existing-channel Settings action are eligible. Reading
uses that action's ``undefined`` open flag; the enable/disable action is never
used. Full settings stay in memory and must not be included in task logs.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup, Tag

from backend.core.mabang_client import MabangApiError, MabangClient

BASE_URL = "https://900853.private.mabangerp.com"
PAGE_PATH = "/index.php?mod=logisticscompany.list"
LIST_PATH = "/index.php?mod=logisticscompany.newLogisticsList&mkey="
CHANNELS_PATH = "/index.php?mod=logisticscompany.findLogisticsChannel&mkey="
SETTINGS_PATH = "/index.php?mod=logisticscompany.logisticsIsEnable&mkey="
SAVE_PATH = "/index.php?mod=logisticscompany.updateLogisticsChannel&mkey="
NUMBER_FIELDS = {"length": "declareLength", "width": "declareWidth", "height": "declareHeight", "weight": "declareWeight"}
SWITCH_FIELDS = {"auto_fill_dimensions": "channelVolumeWeight", "prefer_channel_weight": "isChannelDeclare", "prefer_fixed_volume": "declareSizeConfig"}
MANAGED_FIELDS = frozenset((*NUMBER_FIELDS.values(), *SWITCH_FIELDS.values()))


class TemuShippingGatewayError(MabangApiError):
    pass


def _fail(message: str) -> None:
    # HTTP/library exception text can contain request credentials or settings.
    raise TemuShippingGatewayError(message) from None


def _identifier(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]*", text):
        _fail("物流渠道标识无效，请重新查询渠道")
    return text


def channel_key(channel: Mapping[str, Any]) -> str:
    return ":".join(_identifier(channel.get(key)) for key in ("logistics_id", "my_logistics_id", "channel_id"))


def _decimal(value: Any, *, positive: bool = False) -> str:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value).strip())
        if not number.is_finite() or (positive and number <= 0):
            raise ValueError
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if number == 0 else text
    except (InvalidOperation, ValueError, TypeError):
        _fail("长、宽、高和申报重量必须是有效正数")


def desired_values(dimensions: Any) -> dict[str, Any]:
    values = {key: dimensions.get(key) if isinstance(dimensions, Mapping) else getattr(dimensions, key, None) for key in NUMBER_FIELDS}
    return {**{key: _decimal(value, positive=True) for key, value in values.items()}, **{key: True for key in SWITCH_FIELDS}}


def _fingerprint(pairs: tuple[tuple[str, str], ...]) -> str:
    protected = sorted((key, value) for key, value in pairs if key not in MANAGED_FIELDS)
    return hashlib.sha256(json.dumps(protected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChannelSettingsSnapshot:
    channel_key: str
    managed_values: dict[str, Any]
    other_fingerprint: str
    form_pairs: tuple[tuple[str, str], ...] = field(repr=False)


def settings_match(snapshot: ChannelSettingsSnapshot, desired: Mapping[str, Any], original_snapshot: ChannelSettingsSnapshot | None = None) -> bool:
    return snapshot.managed_values == dict(desired) and (
        original_snapshot is None or (
            snapshot.channel_key == original_snapshot.channel_key
            and snapshot.other_fingerprint == original_snapshot.other_fingerprint
        )
    )


def parse_channel_rows(html: str, company: Mapping[str, Any]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select("#user-login, input[name=password]"):
        _fail("马帮登录已失效，请重新登录")
    if soup.select(".pagination a"):
        _fail("物流渠道列表出现分页，无法确认完整结果，请重新检查页面")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for button in soup.select(".channelSetting"):
        td = button.find_parent("td")
        tr = button.find_parent("tr")
        name = tr.select_one(".text-accent[data-id]") if tr else None
        if td is None or name is None:
            _fail("物流渠道设置按钮结构已变化，停止查询")
        if td.get("data-open") not in (None, "", "undefined") or td.get("data-newopen") not in (None, "", "undefined"):
            _fail("物流渠道设置按钮携带启停参数，停止查询")
        channel = {
            "channel_id": _identifier(td.get("data-channeliid")),
            "channel_name": name.get_text(" ", strip=True),
            "logistics_id": _identifier(td.get("data-id")),
            "my_logistics_id": _identifier(td.get("data-mylogisticid")),
            "source": str(td.get("data-source") or ""),
            "logistics_name": str(company.get("logisticsName") or ""),
            "country_code": str(name.get("data-countrycode") or ""),
        }
        if (channel["logistics_id"] != str(company.get("id")) or channel["my_logistics_id"] != str(company.get("myLogisticsId"))
                or channel["channel_id"] != str(name.get("data-id")) or channel["source"] != "1" or not channel["channel_name"]):
            _fail("物流渠道身份不一致，停止查询")
        # The settings action does not carry the state. Read its own row's
        # switch cell; OFF channels can still have a hidden Settings action.
        # Mabang fragments can omit </tr>, so descendant-wide selectors would
        # accidentally include the switches from following rows.
        state_cells = [cell for cell in tr.find_all("td", recursive=False) if cell.has_attr("data-open")]
        if len(state_cells) != 1:
            _fail("无法确定物流渠道的开启状态，请重新检查页面")
        state_cell = state_cells[0]
        for attribute, key in (("data-channeliid", "channel_id"), ("data-id", "logistics_id"),
                               ("data-mylogisticid", "my_logistics_id"), ("data-source", "source")):
            if str(state_cell.get(attribute) or "") != channel[key]:
                _fail("物流渠道开关与设置入口的身份不一致")
        switches = state_cell.select(".toggle-switch")
        state = str(state_cell.get("data-open") or "")
        if len(switches) != 1 or state not in {"1", "2"}:
            _fail("物流渠道开启状态无法识别，请重新检查页面")
        if ("active" in switches[0].get("class", [])) != (state == "1"):
            _fail("物流渠道开关状态不一致，请重新查询")
        if state == "2":
            continue
        channel["enabled_state"] = "1"
        key = channel_key(channel)
        if key in seen:
            _fail("物流渠道列表包含重复记录，无法确定顺序")
        seen.add(key)
        rows.append(channel)
    return rows


def _disabled(element: Tag) -> bool:
    return element.has_attr("disabled") or any(parent.has_attr("disabled") for parent in element.parents if isinstance(parent, Tag) and parent.name == "fieldset")


def _successful_controls(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for element in soup.select("input[name],select[name],textarea[name]"):
        name = str(element.get("name") or "")
        if not name or _disabled(element):
            continue
        kind = str(element.get("type") or "text").lower()
        if element.name == "input":
            if kind in {"submit", "reset", "button", "image", "file"} or (kind in {"checkbox", "radio"} and not element.has_attr("checked")):
                continue
            values = [str(element.get("value", "on" if kind in {"checkbox", "radio"} else ""))]
        elif element.name == "textarea":
            values = [re.sub(r"\r?\n", "\r\n", element.get_text())]
        else:
            options = [option for option in element.select("option") if not option.has_attr("disabled") and not (option.parent.name == "optgroup" and option.parent.has_attr("disabled"))]
            selected = [option for option in options if option.has_attr("selected")]
            if not element.has_attr("multiple"):
                selected = selected[-1:] if selected else options[:1]
            values = [str(option.get("value", option.get_text())) for option in selected]
        pairs.extend((name, value) for value in values)
    return pairs


def _hydrate_warehouses(soup: BeautifulSoup, payload: Mapping[str, Any]) -> None:
    """Reproduce selectize's saved selections, which are absent from channelData."""
    supplied = payload.get("temuBtWarehouse") or []
    defaults = payload.get("isDefultWarehouse") or []
    controls = soup.select('select[name^="warehousesId-"]')
    selects = {str(element.get("name")): element for element in controls}
    if not isinstance(supplied, list) or not isinstance(defaults, list):
        _fail("渠道仓库配置格式异常")
    if len(selects) != len(supplied) or len(selects) != len(controls):
        _fail("渠道仓库控件与返回数据不一致，无法保留原设置")
    seen: set[str] = set()
    for item in supplied:
        if not isinstance(item, dict):
            _fail("渠道仓库配置格式异常")
        shop = _identifier(item.get("shopId"))
        name = f"warehousesId-{shop}"
        if name not in selects or name in seen:
            _fail("渠道仓库标识重复或缺失")
        seen.add(name)
        choices = item.get("warehousesId") or []
        if not isinstance(choices, list) or any(not isinstance(choice, dict) or not choice.get("warehouseId") for choice in choices):
            _fail("渠道仓库选项无效")
        available = {str(choice["warehouseId"]) for choice in choices}
        selected = next((str(default.get("warehousesId") or "") for default in defaults if isinstance(default, dict) and str(default.get("shopId")) == shop and default.get("warehousesId")), "")
        if selected and selected not in available:
            _fail("原渠道仓库已不在可选列表，需先在马帮处理")
        element = selects[name]
        element.clear()
        option = soup.new_tag("option", value=selected)
        option["selected"] = "selected"
        element.append(option)


def parse_settings(payload: Mapping[str, Any], channel: Mapping[str, Any]) -> ChannelSettingsSnapshot:
    if payload.get("success") not in (True, 1, "1") or not isinstance(payload.get("channelData"), str):
        _fail("读取马帮渠道设置失败，请检查登录状态和权限")
    if str(payload.get("addChannelId") or "") != str(channel["channel_id"]):
        _fail("马帮返回的渠道身份与请求不一致")
    for key, value in payload.items():
        if key not in {"temuBtWarehouse", "isDefultWarehouse"} and isinstance(value, (list, dict)) and value:
            _fail("渠道返回了未适配的动态配置，无法完整保留原设置")
    soup = BeautifulSoup(payload["channelData"], "html.parser")
    # Response fragments have no outer form. Never evaluate embedded scripts.
    if soup.select("script, form, #user-login"):
        _fail("渠道设置页面结构已变化，无法安全保留原设置")
    managed: dict[str, Any] = {}
    for key, name in {**NUMBER_FIELDS, **SWITCH_FIELDS}.items():
        controls = soup.select(f'input[name="{name}"]')
        if len(controls) != 1 or _disabled(controls[0]):
            _fail("渠道缺少可编辑的申报尺寸、重量或开关字段")
        control = controls[0]
        if key in SWITCH_FIELDS:
            if control.get("type") != "checkbox" or str(control.get("value")) != "1":
                _fail("渠道申报开关的取值已变化")
            managed[key] = control.has_attr("checked")
        else:
            managed[key] = _decimal(control.get("value") or "0")
    save_calls = [re.fullmatch(r"updateLogisticsChannel\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*;?", str(element.get("onclick"))) for element in soup.select("[onclick]") if "updateLogisticsChannel(" in str(element.get("onclick"))]
    if len(save_calls) != 1 or save_calls[0] is None or save_calls[0].groups() != (str(channel["channel_id"]), str(channel["logistics_id"]), "1"):
        _fail("渠道保存按钮与查询身份不一致")
    _hydrate_warehouses(soup, payload)
    pairs = _successful_controls(soup)

    def checked(name: str, value: str | None = None) -> bool:
        return any(element.has_attr("checked") and (value is None or str(element.get("value")) == value) for element in soup.select(f'input[name="{name}"]'))

    def value(selector: str, *, attribute: str = "value", missing: str = "undefined") -> str:
        element = soup.select_one(selector)
        if element is None:
            return missing
        if element.name == "select" and attribute == "value":
            return next((v for k, v in pairs if k == element.get("name")), missing)
        return str(element.get(attribute, missing))

    appended: dict[str, str] = {"id": str(channel["channel_id"]), "source": "1"}
    for index, section in enumerate(("address-message", "bill-message", "customs-message"), 1):
        appended[f"labelId{index}"] = value(f"#{section} .active", attribute="data-id") if checked(f"print-type{index}", "2") else ""
    for index in (1, 2):
        appended[f"address{index}Id"] = value(f"#default-address{index}", attribute="data-id")
    for name, label in (("address", "addressMessage"), ("custom", "billMessage"), ("delivery", "customsMessage")):
        appended[name] = "1" if checked("printing-type", label) else "2"
    for name in ("hideTracknumber", "isComboSkuDeclare", "dhlDeclareAuto", "supportRemoteAreas"):
        appended[name] = "1" if checked(name) else "2"
    merge = soup.select_one("#isMergeSkuDeclareSwitch")
    if merge is not None:
        selected = next((str(element.get("value")) for element in soup.select('input[name="mergeSkuDeclareType"][checked]')), "1")
        appended["isMergeSkuDeclare"] = selected if merge.has_attr("checked") else "0"
    else:
        appended["isMergeSkuDeclare"] = "1" if checked("isMergeSkuDeclare") else "2"
    appended["isReplaceLabel"] = "1" if checked("isReplaceLabel") else "0"
    appended["imgUrl"] = value(".custom-drop", attribute="data-url")
    # #previewLabel lives in the outer logistics page, outside channelData;
    # its untouched data-url is the empty string (verified against UI capture).
    appended["pdfUrl"] = value("#previewLabel", attribute="data-url", missing="")
    appended["orderNumberFlag"] = value("#orderNumberFlag", missing="0") or "0"
    # loadOpenAddress() mirrors visibility into the hidden field outside the form.
    appended["openAddressFlag"] = str(payload.get("openAddressFlag")) if payload.get("isShowAddress") and not (str(payload.get("setPageHideAddress")) == "1" and str(payload.get("openAddressFlag")) == "2") else "2"
    if appended["openAddressFlag"] not in {"1", "2"}:
        _fail("渠道地址设置缺失，无法保留原设置")
    # The UI appends these scalar values after serialize(); PHP uses the last.
    full = tuple((key, val) for key, val in pairs if key not in appended) + tuple(appended.items())
    return ChannelSettingsSnapshot(channel_key(channel), managed, _fingerprint(full), full)


def build_save_pairs(snapshot: ChannelSettingsSnapshot, dimensions: Any) -> tuple[tuple[str, str], ...]:
    desired = desired_values(dimensions)
    updates = {name: str(desired[key]) for key, name in NUMBER_FIELDS.items()}
    updates.update({name: "1" for name in SWITCH_FIELDS.values()})
    return tuple((key, value) for key, value in snapshot.form_pairs if key not in MANAGED_FIELDS) + tuple(updates.items())


class TemuShippingGateway:
    def __init__(self, username: str, password: str, *, client: MabangClient | None = None):
        self._username, self._password = username, password
        self._owns_client = client is None
        self._client = client or MabangClient(BASE_URL)

    def __enter__(self) -> "TemuShippingGateway":
        try:
            if self._owns_client:
                self._client.login(self._username, self._password)
        except Exception:
            self.close()
            _fail("马帮登录失败，请检查账号或验证要求")
        finally:
            self._password = ""
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, path: str, pairs: Any) -> dict[str, Any]:
        try:
            result = self._client.post_form_json(path, form_data=pairs, referer=PAGE_PATH, context="TEMU 物流渠道请求")
        except Exception:
            _fail("马帮渠道请求失败，请检查网络、登录状态及权限")
        if not isinstance(result, dict):
            _fail("马帮渠道接口返回格式异常")
        return result

    def _company_channels(self, company: Mapping[str, Any]) -> list[dict[str, Any]]:
        detail = self._post(CHANNELS_PATH, {"isPlatformLogisticsPage": "1", "countryCode": "ALL", "routeType": "1", "id": _identifier(company.get("id")), "source": "1", "myLogisticsId": _identifier(company.get("myLogisticsId")), "logisticsNameSearch": ""})
        if detail.get("success") not in (True, 1, "1") or not isinstance(detail.get("message"), str):
            _fail("读取 TEMU 物流渠道列表失败")
        return parse_channel_rows(detail["message"], company)

    def list_channels(self) -> list[dict[str, Any]]:
        result = self._post(LIST_PATH, {"routeType": "1", "isOnlineShipment": "1", "currentPage": "0", "pageSize": "200", "isOpen": "0", "searchtype": "1", "logisticsName": "TEMU"})
        data = result.get("data")
        if str(result.get("code")) != "200" or not isinstance(data, dict) or not isinstance(data.get("list"), list):
            _fail("查询 TEMU 平台物流失败")
        companies = data["list"]
        try:
            complete = int(data["totalCount"]) == len(companies) and int(data["totalPage"]) <= 1
        except (KeyError, ValueError, TypeError):
            complete = False
        if not complete:
            _fail("平台物流查询结果不完整，停止处理")
        channels: list[dict[str, Any]] = []
        seen: set[str] = set()
        for company in companies:
            if not isinstance(company, dict) or "temu" not in str(company.get("logisticsName") or "").casefold():
                _fail("平台物流搜索返回非 TEMU 项目，停止处理")
            if not company.get("myLogisticsId"):
                continue
            for channel in self._company_channels(company):
                key = channel_key(channel)
                if key in seen:
                    _fail("平台物流返回重复渠道，无法确定顺序")
                seen.add(key)
                channels.append(channel)
        return channels

    def read_settings(self, channel: Mapping[str, Any]) -> ChannelSettingsSnapshot:
        channel_key(channel)
        if str(channel.get("source")) != "1" or channel.get("enabled_state") != "1":
            _fail("仅支持已开启的平台物流渠道，请重新查询")
        result = self._post(SETTINGS_PATH, {"id": str(channel["logistics_id"]), "channelIid": str(channel["channel_id"]), "isOpen": "undefined", "myLogisticsId": str(channel["my_logistics_id"]), "isNewOpen": "undefined", "isZifaFlag": "2"})
        return parse_settings(result, channel)

    def save_settings(self, channel: Mapping[str, Any], snapshot: ChannelSettingsSnapshot, dimensions: Any) -> dict[str, Any]:
        if channel_key(channel) != snapshot.channel_key or str(channel.get("source")) != "1" or channel.get("enabled_state") != "1":
            _fail("保存渠道与原设置快照不一致")
        if _fingerprint(snapshot.form_pairs) != snapshot.other_fingerprint:
            _fail("渠道设置快照已被改动，请重新查询")
        pairs = build_save_pairs(snapshot, dimensions)
        # A channel can be switched off while earlier rows are being saved.
        # Recheck this company's live list immediately before each write.
        try:
            current = self._company_channels({"id": channel["logistics_id"], "myLogisticsId": channel["my_logistics_id"], "logisticsName": channel.get("logistics_name", "TEMU")})
        except Exception:
            _fail("提交前无法确认渠道当前开启状态，未提交修改，请重新查询")
        if snapshot.channel_key not in {channel_key(item) for item in current}:
            _fail("渠道已关闭或已从开启列表移除，未提交修改，请重新查询")
        result = self._post(SAVE_PATH, pairs)
        if result.get("success") not in (True, 1, "1"):
            _fail("马帮未确认渠道保存成功，请读回检查后重试")
        return {"success": True}
