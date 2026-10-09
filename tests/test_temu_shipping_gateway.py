from __future__ import annotations

import copy
import traceback
import unittest
from dataclasses import replace
from unittest.mock import Mock

from backend.services import temu_shipping_gateway as gateway


CHANNEL = {"channel_id": "101", "channel_name": "TEMU A", "logistics_id": "20", "my_logistics_id": "30", "source": "1", "enabled_state": "1"}
COMPANY = {"id": 20, "myLogisticsId": 30, "logisticsName": "TEMU 线上发货"}
DIMENSIONS = {"length": "12.00", "width": "10", "height": "8", "weight": "160.000"}


def channel_rows(ids: tuple[int, ...] = (101, 102), *, off_ids: tuple[int, ...] = ()) -> str:
    return "<table>" + "".join(
        f'<tr><td><span class="text-accent" data-id="{identifier}" data-countrycode="OTHER">渠道 {identifier}</span></td>'
        f'<td data-channelIid="{identifier}" data-id="20" data-mylogisticid="30" data-source="1" data-open="{2 if identifier in off_ids else 1}">'
        f'<div class="toggle-switch{chr(32) + "active" if identifier not in off_ids else ""}"><span>ON</span><span>OFF</span></div></td>'
        f'<td data-channelIid="{identifier}" data-id="20" data-mylogisticid="30" data-source="1">'
        '<a class="channelSetting">设置</a></td></tr>' for identifier in ids
    ) + '<tr><td>未启用的渠道</td><td><input data-open="2" class="switchery-colored"></td></tr></table>'


def settings_payload() -> dict:
    return {
        "success": True, "addChannelId": 101, "isShowAddress": True, "openAddressFlag": 1, "setPageHideAddress": 2,
        "temuBtWarehouse": [
            {"shopId": "501", "warehousesId": [{"warehouseId": "w-1", "warehouseName": "Warehouse A"}, {"warehouseId": "w-2", "warehouseName": "Warehouse B"}]},
            {"shopId": "502", "warehousesId": [{"warehouseId": "w-3", "warehouseName": "Warehouse C"}]},
        ],
        "isDefultWarehouse": [{"shopId": "501", "warehousesId": "w-2"}],
        "channelData": '''
          <input name="declareLength" value="10.00"><input name="declareWidth" value="9">
          <input name="declareHeight" value="8"><input name="declareWeight" value="120.0">
          <input type="checkbox" name="channelVolumeWeight" value="1" checked>
          <input type="checkbox" name="isChannelDeclare" value="1">
          <input type="checkbox" name="declareSizeConfig" value="1" checked>
          <input name="apiToken" value="PRIVATE-SYNTHETIC-TOKEN" type="hidden">
          <input name="untouched[]" value="first"><input name="untouched[]" value="second">
          <input name="disabledValue" value="excluded" disabled>
          <input name="uncheckedValue" value="excluded" type="checkbox">
          <input name="buttonValue" type="submit" value="excluded">
          <textarea name="notes">one\ntwo</textarea>
          <select name="shippingFeeTypeId"><option value="fee-1">one</option><option value="fee-2" selected>two</option></select>
          <select name="multi[]" multiple><option value="a" selected>A</option><option value="b" selected>B</option><option value="disabled" disabled selected>D</option></select>
          <select name="warehousesId-501"></select><select name="warehousesId-502"></select>
          <input name="hideTracknumber" type="checkbox" value="2" checked>
          <input name="isComboSkuDeclare" type="checkbox" value="" checked>
          <input name="isMergeSkuDeclare" type="checkbox">
          <input name="printing-type" type="checkbox" value="addressMessage" checked>
          <input name="printing-type" type="checkbox" value="billMessage">
          <input name="printing-type" type="checkbox" value="customsMessage" checked>
          <input name="print-type1" type="radio" value="2" checked>
          <input name="print-type2" type="radio" value="1" checked>
          <input name="print-type3" type="radio" value="1" checked>
          <div id="address-message"><div class="active" data-id="label-7"></div></div>
          <div id="default-address1" data-id="sender-8"></div>
          <div id="default-address2" data-id="pickup-9"></div>
          <select id="orderNumberFlag" name="orderNumberFlag"><option value="3" selected>3</option></select>
          <button onclick="updateLogisticsChannel(101,20,1)">保存</button>
        ''',
    }


class TemuChannelParsingTests(unittest.TestCase):
    def test_off_rows_with_settings_action_are_excluded_without_using_excel_slots(self):
        channels = gateway.parse_channel_rows(channel_rows((103, 102, 101), off_ids=(102,)), COMPANY)
        self.assertEqual([channel["channel_id"] for channel in channels], ["103", "101"])
        self.assertTrue(all(channel["enabled_state"] == "1" for channel in channels))

    def test_switch_state_is_scoped_to_current_row_with_omitted_closing_tags(self):
        html = channel_rows((103, 102, 101), off_ids=(102,)).replace("</tr>", "")
        channels = gateway.parse_channel_rows(html, COMPANY)
        self.assertEqual([channel["channel_id"] for channel in channels], ["103", "101"])

    def test_unknown_conflicting_or_wrong_channel_switch_state_is_rejected(self):
        html = channel_rows((101,))
        variants = (
            html.replace('data-open="1"', 'data-open="unknown"'),
            html.replace('data-open="1"', ''),
            html.replace('toggle-switch active', 'toggle-switch'),
            html.replace('data-channelIid="101" data-id="20" data-mylogisticid="30" data-source="1" data-open=',
                         'data-channelIid="999" data-id="20" data-mylogisticid="30" data-source="1" data-open='),
        )
        for variant in variants:
            with self.subTest(variant=variants.index(variant)), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_channel_rows(variant, COMPANY)

    def test_all_settings_rows_off_produces_no_targets(self):
        self.assertEqual(gateway.parse_channel_rows(channel_rows(off_ids=(101, 102)), COMPANY), [])

    def test_only_existing_settings_rows_in_page_order(self):
        channels = gateway.parse_channel_rows(channel_rows((103, 101)), COMPANY)
        self.assertEqual([channel["channel_id"] for channel in channels], ["103", "101"])
        self.assertEqual(channels[0]["country_code"], "OTHER")

    def test_duplicate_rows_are_rejected(self):
        with self.assertRaises(gateway.TemuShippingGatewayError):
            gateway.parse_channel_rows(channel_rows((101, 101)), COMPANY)

    def test_settings_action_cannot_be_enable_disable_action(self):
        for attribute in ('data-open="2"', 'data-newopen="1"'):
            with self.subTest(attribute=attribute), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_channel_rows(channel_rows().replace('data-source="1"', f'data-source="1" {attribute}'), COMPANY)

    def test_wrong_channel_or_company_identity_rejected(self):
        for replacement in ('data-mylogisticid="999"', 'data-source="2"'):
            original = 'data-mylogisticid="30"' if "mylogisticid" in replacement else 'data-source="1"'
            with self.subTest(replacement=replacement), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_channel_rows(channel_rows().replace(original, replacement), COMPANY)

    def test_login_page_or_pagination_cannot_be_empty_success(self):
        for html in ('<form id="user-login"></form>', channel_rows() + '<ul class="pagination"><li><a>2</a></li></ul>'):
            with self.subTest(html=html[:40]), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_channel_rows(html, COMPANY)


class TemuSettingsTests(unittest.TestCase):
    def setUp(self):
        self.payload = settings_payload()
        self.snapshot = gateway.parse_settings(self.payload, CHANNEL)

    def test_reads_all_managed_values_and_hydrates_saved_warehouses(self):
        self.assertEqual(self.snapshot.managed_values, {"length": "10", "width": "9", "height": "8", "weight": "120", "auto_fill_dimensions": True, "prefer_channel_weight": False, "prefer_fixed_volume": True})
        fields = dict(self.snapshot.form_pairs)
        self.assertEqual(fields["warehousesId-501"], "w-2")
        self.assertEqual(fields["warehousesId-502"], "")

    def test_only_seven_managed_fields_change(self):
        output = gateway.build_save_pairs(self.snapshot, DIMENSIONS)
        protected = lambda pairs: sorted((key, value) for key, value in pairs if key not in gateway.MANAGED_FIELDS)
        self.assertEqual(protected(output), protected(self.snapshot.form_pairs))
        expected = {"declareLength": "12", "declareWidth": "10", "declareHeight": "8", "declareWeight": "160", "channelVolumeWeight": "1", "isChannelDeclare": "1", "declareSizeConfig": "1"}
        self.assertEqual({key: value for key, value in output if key in gateway.MANAGED_FIELDS}, expected)
        self.assertEqual(sum(key in gateway.MANAGED_FIELDS for key, _ in output), 7)

    def test_successful_controls_preserve_repeated_fields_and_skip_disabled(self):
        pairs = self.snapshot.form_pairs
        self.assertEqual([value for key, value in pairs if key == "untouched[]"], ["first", "second"])
        self.assertEqual([value for key, value in pairs if key == "multi[]"], ["a", "b"])
        self.assertEqual(dict(pairs)["notes"], "one\r\ntwo")
        self.assertFalse({"disabledValue", "uncheckedValue", "buttonValue"} & set(dict(pairs)))

    def test_frontend_appended_fields_do_not_reset_other_settings(self):
        fields = dict(self.snapshot.form_pairs)
        expected = {"hideTracknumber": "1", "isComboSkuDeclare": "1", "isMergeSkuDeclare": "2", "labelId1": "label-7", "labelId2": "", "address1Id": "sender-8", "address2Id": "pickup-9", "address": "1", "custom": "2", "delivery": "1", "orderNumberFlag": "3", "openAddressFlag": "1", "isReplaceLabel": "0"}
        for key, value in expected.items():
            self.assertEqual(fields[key], value, key)
            self.assertEqual(sum(name == key for name, _ in self.snapshot.form_pairs), 1, key)

    def test_outer_page_pdf_preview_starts_empty_when_absent_from_fragment(self):
        fields = dict(self.snapshot.form_pairs)
        self.assertEqual(fields["pdfUrl"], "")
        self.assertEqual(fields["imgUrl"], "undefined")
        self.payload["channelData"] += '<button id="previewLabel" data-url="synthetic-label.pdf"></button>'
        snapshot = gateway.parse_settings(self.payload, CHANNEL)
        self.assertEqual(dict(snapshot.form_pairs)["pdfUrl"], "synthetic-label.pdf")

    def test_readback_checks_protected_settings_and_channel_identity(self):
        desired = gateway.desired_values(DIMENSIONS)
        after = replace(self.snapshot, managed_values=desired)
        self.assertTrue(gateway.settings_match(after, desired, self.snapshot))
        self.assertFalse(gateway.settings_match(replace(after, other_fingerprint="changed"), desired, self.snapshot))
        self.assertFalse(gateway.settings_match(replace(after, channel_key="wrong"), desired, self.snapshot))
        self.assertFalse(gateway.settings_match(self.snapshot, desired))

    def test_secret_not_in_snapshot_repr(self):
        self.assertNotIn("PRIVATE-SYNTHETIC-TOKEN", repr(self.snapshot))

    def test_stale_default_warehouse_is_not_cleared(self):
        self.payload["isDefultWarehouse"][0]["warehousesId"] = "deleted-warehouse"
        with self.assertRaises(gateway.TemuShippingGatewayError):
            gateway.parse_settings(self.payload, CHANNEL)

    def test_incomplete_or_duplicate_warehouse_data_rejected(self):
        for transform in (lambda items: items[:-1], lambda items: [items[0], items[0]]):
            payload = copy.deepcopy(self.payload)
            payload["temuBtWarehouse"] = transform(payload["temuBtWarehouse"])
            with self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_settings(payload, CHANNEL)

    def test_new_dynamic_settings_are_not_silently_omitted(self):
        self.payload["futureAddressConfiguration"] = [{"synthetic": "value"}]
        with self.assertRaises(gateway.TemuShippingGatewayError):
            gateway.parse_settings(self.payload, CHANNEL)

    def test_wrong_settings_identity_or_missing_managed_fields_rejected(self):
        for patch in ({"addChannelId": 102}, {"channelData": self.payload["channelData"].replace('name="declareWeight"', 'name="other"')}, {"channelData": self.payload["channelData"].replace("updateLogisticsChannel(101,20,1)", "updateLogisticsChannel(102,20,1)")}):
            with self.subTest(patch=list(patch)), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.parse_settings({**self.payload, **patch}, CHANNEL)

    def test_invalid_dimensions_never_build_save(self):
        for invalid in (0, -1, "NaN", "Infinity", "", None, True):
            with self.subTest(value=invalid), self.assertRaises(gateway.TemuShippingGatewayError):
                gateway.build_save_pairs(self.snapshot, {**DIMENSIONS, "weight": invalid})

    def test_decimal_precision_above_context_limit_is_preserved(self):
        precise = "12345678901234567890123456789.12345678901234567890123456789"
        values = gateway.desired_values({**DIMENSIONS, "weight": precise + "000"})
        self.assertEqual(values["weight"], precise)
        output = dict(gateway.build_save_pairs(self.snapshot, {**DIMENSIONS, "weight": precise}))
        self.assertEqual(output["declareWeight"], precise)
        self.assertEqual(gateway._decimal("1000.000"), "1000")
        self.assertEqual(gateway._decimal("0.000"), "0")
        self.assertEqual(gateway._decimal("-0.000"), "0")


class TemuGatewayRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.gateway = gateway.TemuShippingGateway("", "", client=self.client)

    def test_injected_logged_in_client_is_not_logged_in_or_closed(self):
        with self.gateway:
            pass
        self.client.login.assert_not_called()
        self.client.close.assert_not_called()

    def test_off_or_unconfirmed_channel_cannot_read_or_save_settings(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        for state in ("2", "", None):
            channel = {**CHANNEL, "enabled_state": state}
            with self.subTest(state=state):
                with self.assertRaises(gateway.TemuShippingGatewayError):
                    self.gateway.read_settings(channel)
                with self.assertRaises(gateway.TemuShippingGatewayError):
                    self.gateway.save_settings(channel, snapshot, DIMENSIONS)
        self.client.post_form_json.assert_not_called()

    def test_list_uses_temu_platform_filter_and_all_countries(self):
        self.client.post_form_json.side_effect = [{"code": 200, "data": {"list": [COMPANY], "totalCount": 1, "totalPage": 1}}, {"success": True, "message": channel_rows()}]
        channels = self.gateway.list_channels()
        self.assertEqual(len(channels), 2)
        calls = self.client.post_form_json.call_args_list
        self.assertEqual(calls[0].args[0], gateway.LIST_PATH)
        self.assertEqual(calls[0].kwargs["form_data"]["logisticsName"], "TEMU")
        self.assertEqual(calls[0].kwargs["form_data"]["isOnlineShipment"], "1")
        self.assertEqual(calls[1].kwargs["form_data"]["countryCode"], "ALL")
        self.assertEqual(calls[1].kwargs["form_data"]["myLogisticsId"], "30")

    def test_incomplete_company_list_stops_before_channel_queries(self):
        self.client.post_form_json.return_value = {"code": 200, "data": {"list": [COMPANY], "totalCount": 201, "totalPage": 2}}
        with self.assertRaises(gateway.TemuShippingGatewayError):
            self.gateway.list_channels()
        self.assertEqual(self.client.post_form_json.call_count, 1)

    def test_read_uses_settings_flags_never_toggle_flags(self):
        self.client.post_form_json.return_value = settings_payload()
        self.gateway.read_settings(CHANNEL)
        request = self.client.post_form_json.call_args
        self.assertEqual(request.args[0], gateway.SETTINGS_PATH)
        self.assertEqual(request.kwargs["form_data"]["isOpen"], "undefined")
        self.assertEqual(request.kwargs["form_data"]["isNewOpen"], "undefined")
        self.assertEqual(request.kwargs["form_data"]["isZifaFlag"], "2")

    def test_save_uses_proven_endpoint_and_returns_no_raw_response(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        self.client.post_form_json.side_effect = [{"success": True, "message": channel_rows()}, {"success": True, "private": "secret"}]
        self.assertEqual(self.gateway.save_settings(CHANNEL, snapshot, DIMENSIONS), {"success": True})
        request = self.client.post_form_json.call_args
        self.assertEqual(request.args[0], gateway.SAVE_PATH)
        self.assertEqual(dict(request.kwargs["form_data"])["apiToken"], "PRIVATE-SYNTHETIC-TOKEN")
        self.assertEqual([call.args[0] for call in self.client.post_form_json.call_args_list], [gateway.CHANNELS_PATH, gateway.SAVE_PATH])

    def test_cached_on_marker_cannot_save_channel_that_is_now_off_or_removed(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        for html in (channel_rows(off_ids=(101,)), channel_rows((102,))):
            self.client.reset_mock()
            self.client.post_form_json.return_value = {"success": True, "message": html}
            with self.assertRaisesRegex(gateway.TemuShippingGatewayError, "已关闭或已从开启列表移除"):
                self.gateway.save_settings(CHANNEL, snapshot, DIMENSIONS)
            self.assertEqual([call.args[0] for call in self.client.post_form_json.call_args_list], [gateway.CHANNELS_PATH])

    def test_unknown_current_state_blocks_save(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        self.client.post_form_json.return_value = {"success": True, "message": channel_rows().replace('data-open="1"', 'data-open="unknown"')}
        with self.assertRaisesRegex(gateway.TemuShippingGatewayError, "无法确认渠道当前开启状态"):
            self.gateway.save_settings(CHANNEL, snapshot, DIMENSIONS)
        self.assertEqual([call.args[0] for call in self.client.post_form_json.call_args_list], [gateway.CHANNELS_PATH])

    def test_second_channel_switched_off_after_first_save_is_not_submitted(self):
        first = gateway.parse_settings(settings_payload(), CHANNEL)
        second_channel = {**CHANNEL, "channel_id": "102"}
        second_payload = settings_payload()
        second_payload["addChannelId"] = 102
        second_payload["channelData"] = second_payload["channelData"].replace("updateLogisticsChannel(101,20,1)", "updateLogisticsChannel(102,20,1)")
        second = gateway.parse_settings(second_payload, second_channel)
        self.client.post_form_json.side_effect = [
            {"success": True, "message": channel_rows()},
            {"success": True},
            {"success": True, "message": channel_rows(off_ids=(102,))},
        ]
        self.gateway.save_settings(CHANNEL, first, DIMENSIONS)
        with self.assertRaisesRegex(gateway.TemuShippingGatewayError, "已关闭或已从开启列表移除"):
            self.gateway.save_settings(second_channel, second, DIMENSIONS)
        calls = self.client.post_form_json.call_args_list
        self.assertEqual([call.args[0] for call in calls], [gateway.CHANNELS_PATH, gateway.SAVE_PATH, gateway.CHANNELS_PATH])
        self.assertEqual(dict(calls[1].kwargs["form_data"])["id"], "101")

    def test_changed_snapshot_or_target_cannot_be_sent(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        for channel, changed in (({**CHANNEL, "channel_id": "102"}, snapshot), (CHANNEL, replace(snapshot, form_pairs=snapshot.form_pairs + (("extra", "changed"),)))):
            with self.assertRaises(gateway.TemuShippingGatewayError):
                self.gateway.save_settings(channel, changed, DIMENSIONS)
        self.client.post_form_json.assert_not_called()

    def test_failed_save_is_not_retried_and_does_not_expose_response(self):
        snapshot = gateway.parse_settings(settings_payload(), CHANNEL)
        self.client.post_form_json.side_effect = [{"success": True, "message": channel_rows()}, RuntimeError("PRIVATE-SYNTHETIC-TOKEN")]
        formatted = ""
        try:
            self.gateway.save_settings(CHANNEL, snapshot, DIMENSIONS)
        except gateway.TemuShippingGatewayError:
            formatted = traceback.format_exc()
        self.assertTrue(formatted)
        self.assertNotIn("PRIVATE-SYNTHETIC-TOKEN", formatted)
        self.assertEqual([call.args[0] for call in self.client.post_form_json.call_args_list], [gateway.CHANNELS_PATH, gateway.SAVE_PATH])


if __name__ == "__main__":
    unittest.main()
