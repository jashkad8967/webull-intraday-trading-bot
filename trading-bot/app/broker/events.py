"""Official SDK order-event subscription entry point."""
from app.config.settings import Settings
def run_order_events(settings: Settings, on_order_update) -> None:
    from webull.trade.events.types import ORDER_STATUS_CHANGED, EVENT_TYPE_ORDER
    from webull.trade.trade_events_client import TradeEventsClient
    client = TradeEventsClient(settings.webull_app_key, settings.webull_app_secret, settings.webull_environment)
    def on_event(event_type, subscribe_type, payload, raw_message):
        if event_type == EVENT_TYPE_ORDER and subscribe_type == ORDER_STATUS_CHANGED: on_order_update(payload)
    client.on_events_message = on_event
    client.do_subscribe([settings.account_id])
