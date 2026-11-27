import asyncio, httpx, json, time, datetime as dt, sys
BASE="https://gateway.polymarket.us"
H=sys.argv[1]; STEP={"15m":900,"1h":3600}[H]; DAYS=int(sys.argv[2])
end=int(dt.datetime(2026,11,27,tzinfo=dt.timezone.utc).timestamp()); start=end-DAYS*86400
out=open(f"/home/user/pm/markets-{H}.jsonl","w")
async def main():
    async with httpx.AsyncClient(headers={"User-Agent":"research-client"},timeout=30) as c:
        meta=None
        for t in range(start,end,STEP):
            slug=f"cpc-btc-updown-{H}-"+dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime("%Y-%m-%d-%H%Mz")
            try:
                h=(await c.get(BASE+"/v1/price-history",params={"symbol":slug,"timestamp.startTimestamp":t,"timestamp.endTimestamp":t+STEP,"fidelity":1})).json()
                await asyncio.sleep(.17)
                s=(await c.get(BASE+f"/v1/markets/{slug}/settlement")).json()
                await asyncio.sleep(.17)
                if meta is None:
                    mm=(await c.get(BASE+"/v1/markets",params={"slug":slug})).json().get("markets") or [{}]
                    meta=dict(fee=mm[0].get("feeCoefficient"),desc=mm[0].get("description","")[:400]); print(meta,flush=True)
                out.write(json.dumps(dict(slug=slug,start=t,history=h.get("history"),settlement=s.get("settlement")))+"\n")
            except Exception as e: print("err",slug,e)
asyncio.run(main())
