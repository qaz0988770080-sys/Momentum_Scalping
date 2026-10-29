"""Offline Taishin preview. Does not load a broker SDK."""
import argparse
import json
from api_gateway_v2_7 import APIGateway, CTSOrderRequest

def main():
    parser = argparse.ArgumentParser(description="台新獨立專案：離線委託預覽")
    parser.add_argument("--symbol", default="2330")
    parser.add_argument("--lots", type=int, default=2)
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--tif", choices=["ROD", "IOC", "FOK"], default="ROD")
    parser.add_argument("--day-trade", choices=["N", "Y"], required=True)
    args = parser.parse_args()
    gateway = APIGateway(mode="OFFLINE", broker="TAISHIN",
                         taishin_day_trade_flag=args.day_trade)
    try:
        request = CTSOrderRequest(account_id="OFFLINE-DEMO", trade_date=20261102,
            trade_type=0, order_type=0, side=1 if args.side == "buy" else 2,
            symbol=args.symbol, quantity=args.lots, price_type=4, price="0",
            condition={"ROD": 0, "FOK": 1, "IOC": 2}[args.tif],
            order_intent_id="MS-DEMO-0001")
        print(json.dumps(gateway.broker.preview_order(request), ensure_ascii=False, indent=2))
    finally:
        gateway.close()

if __name__ == "__main__":
    main()
