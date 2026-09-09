import json

from webull_bot.config import settings
from webull_bot.webull_api import WebullAPI


def main() -> None:
    config = settings()
    config.validate_connection(require_account=False)
    api = WebullAPI(config)
    accounts = api.accounts()
    print("Accounts:")
    print(json.dumps(accounts, indent=2))
    if not config.account_id:
        print("\nCopy an account_id above into ACCOUNT_ID in .env.")
        return
    print("\nBuying power:", api.buying_power())
    print("Positions:")
    print(json.dumps(api.positions(), indent=2))
    first_stock = next((item for item in config.stocks() if item != "ALL"), None)
    if first_stock:
        print(f"{first_stock} quote:")
        print(json.dumps(api.stock_quote(first_stock), indent=2))


if __name__ == "__main__":
    main()
