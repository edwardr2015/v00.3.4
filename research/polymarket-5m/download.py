import asyncio, httpx, json, time, datetime as dt, os, sys
BASE="https://gateway.polymarket.us"; OUT="/home/user/pm"
start=int(dt.datetime(2026,11,1,tzinfo=dt.timezone.utc).timestamp()); end=int(dt.datetime(2026,11,27,tzinfo=dt.timezone.utc).timestamp())
RATE=6.0
class Limiter:
    def __init__(s,r): s.gap=1/r; s.next=time.monotonic(); s.lock=asyncio.Lock()
    async def wait(s):
        async with s.lock:
            now=time.monotonic(); w=s.next-now
            if w>0: await asyncio.sleep(w)
            s.next=max(now,s.next)+s.gap
lim=Limiter(RATE)
async def get(c,path,params):
    for attempt in range(5):
        await lim.wait()
        try:
            r=await c.get(BASE+path,params=params,timeout=30)
            if r.status_code==429: await asyncio.sleep(5*(attempt+1)); continue
            if r.status_code>=500: await asyncio.sleep(2); continue
            r.raise_for_status(); return r.json()
        except httpx.HTTPError: await asyncio.sleep(2)
    return None
async def main():
    async with httpx.AsyncClient(headers={"User-Agent":"research-client"}) as c:
        # BRTI by day
        for d in range(start,end,86400):
            f=f"{OUT}/brti-{d}.json"
            if os.path.exists(f): continue
            j=await get(c,"/v1/asset-prices/history",{"assets":"crypto:btc","from":d-120,"to":d+86400,"bucket":"ASSET_PRICE_BUCKET_1S"})
            if j: json.dump([(x["timestamp"],float(x["close"])) for x in j["series"][0]["candles"]],open(f,"w"))
        print("brti done",flush=True)
        f=f"{OUT}/markets.jsonl"; done=set()
        if os.path.exists(f):
            done={json.loads(l)["slug"] for l in open(f)}
        out=open(f,"a"); q=asyncio.Queue()
        for t in range(start,end,300):
            slug="cpc-btc-updown-5m-"+dt.datetime.fromtimestamp(t,dt.timezone.utc).strftime("%Y-%m-%d-%H%Mz")
            if slug not in done: q.put_nowait((t,slug))
        n=0
        async def worker():
            nonlocal n
            while not q.empty():
                t,slug=await q.get()
                h=await get(c,"/v1/price-history",{"symbol":slug,"timestamp.startTimestamp":t-60,"timestamp.endTimestamp":t+300,"fidelity":1})
                s=await get(c,f"/v1/markets/{slug}/settlement",{})
                out.write(json.dumps(dict(slug=slug,start=t,history=(h or {}).get("history"),settlement=(s or {}).get("settlement")))+"\n"); out.flush()
                n+=1
                if n%500==0: print(n,"markets",flush=True)
        await asyncio.gather(*[worker() for _ in range(4)])
asyncio.run(main())
