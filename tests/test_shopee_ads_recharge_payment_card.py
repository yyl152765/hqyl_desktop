import unittest

from backend.services import shopee_ads_recharge as service


class ShopeeAdsRechargePaymentCardTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "site_code": "my",
            "company": "company-a",
            "username": "user-a",
            "password": "password-a",
            "recharge_mode": "custom",
            "custom_recharge_items": [{"store_name": "马来259", "amount": "157.6"}],
        }
        payload.update(overrides)
        return payload

    def test_payment_card_type_accepts_world(self):
        query = service.validate_shopee_ads_payload(self._payload(payment_card_type="WORLD"))

        self.assertEqual(query.payment_card_type, "world")

    def test_payment_card_type_defaults_to_payoneer_for_unknown_value(self):
        query = service.validate_shopee_ads_payload(self._payload(payment_card_type="unknown"))

        self.assertEqual(query.payment_card_type, "payoneer")

    def test_sms_verification_defaults_to_skipped(self):
        query = service.validate_shopee_ads_payload(self._payload())

        self.assertFalse(query.sms_verification_required)

    def test_sms_verification_can_be_enabled(self):
        query = service.validate_shopee_ads_payload(
            self._payload(sms_verification_required=True)
        )

        self.assertTrue(query.sms_verification_required)


if __name__ == "__main__":
    unittest.main()
