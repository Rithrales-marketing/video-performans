#!/usr/bin/env python3
"""
Meta Ads -> data.json -> Supabase Storage senkronizasyonu.

GitHub Actions icinde calisir. Gerekli ortam degiskenleri (GitHub Secrets):
  META_TOKEN            System User access token (ads_read yetkisi)
  META_AD_ACCOUNT       Reklam hesabi kimligi, act_ onekiyle veya onsuz
  SUPABASE_URL          https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY  service_role anahtari
  SUPABASE_BUCKET       varsayilan: panel

Meta'dan gelen: harcama, gosterim, hook rate, hold rate, lead
CRM'den gelen (elle):  randevu, islem   -> bu alanlara dokunulmaz.
"""
import os, re, sys, json, datetime, urllib.parse, urllib.request

API = "https://graph.facebook.com/v21.0"
TOKEN   = os.environ["META_TOKEN"]
ACCOUNT = os.environ["META_AD_ACCOUNT"]
if not ACCOUNT.startswith("act_"): ACCOUNT = "act_" + ACCOUNT
SB_URL  = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET  = os.environ.get("SUPABASE_BUCKET", "panel")
KAMPANYA_ONEK = os.environ.get("META_KAMPANYA_ONEK", "TEST-G")

KOD_RE = re.compile(r"V\d{2}-\d{2}-\d{2}")

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=90) as r:
        return json.loads(r.read().decode())

def sayfalar(url):
    while url:
        d = get(url)
        for x in d.get("data", []): yield x
        url = d.get("paging", {}).get("next")

def aksiyon(liste, tip):
    for a in liste or []:
        if a.get("action_type") == tip:
            try: return float(a.get("value", 0))
            except (TypeError, ValueError): return 0.0
    return None

def ilk_dolu(satir, alanlar):
    """Meta surumden surume alan adi degistiriyor; ilk dolu olani kullan."""
    for f in alanlar:
        v = satir.get(f)
        if v:
            try: return float(v[0]["value"])
            except (KeyError, IndexError, TypeError, ValueError): pass
    return None

def meta_verisi(baslangic, bitis):
    alanlar = ",".join([
        "adset_name","campaign_name","spend","impressions","actions",
        "video_3_sec_watched_actions","video_play_actions",
        "video_p25_watched_actions","video_p75_watched_actions",
    ])
    q = urllib.parse.urlencode({
        "level":"adset","fields":alanlar,"limit":"500",
        "time_range":json.dumps({"since":baslangic,"until":bitis}),
        "access_token":TOKEN,
    })
    sonuc = {}
    for r in sayfalar(f"{API}/{ACCOUNT}/insights?{q}"):
        kamp = r.get("campaign_name","")
        if KAMPANYA_ONEK and not kamp.startswith(KAMPANYA_ONEK): continue
        m = KOD_RE.search(r.get("adset_name",""))
        if not m: continue
        kod = m.group(0)
        gosterim = float(r.get("impressions") or 0)
        hook3 = ilk_dolu(r, ["video_3_sec_watched_actions","video_play_actions"])
        hold  = ilk_dolu(r, ["video_p75_watched_actions"])
        lead  = aksiyon(r.get("actions"), "onsite_conversion.total_messaging_connection")
        if lead is None:
            lead = aksiyon(r.get("actions"), "onsite_conversion.messaging_conversation_started_7d")
        onceki = sonuc.get(kod, {"harcama":0.0,"gosterim":0.0,"hook_n":0.0,"hold_n":0.0,"lead":0.0})
        sonuc[kod] = {
            "harcama": onceki["harcama"] + float(r.get("spend") or 0),
            "gosterim": onceki["gosterim"] + gosterim,
            "hook_n": onceki["hook_n"] + (hook3 or 0),
            "hold_n": onceki["hold_n"] + (hold or 0),
            "lead": onceki["lead"] + (lead or 0),
            "asama": "G2" if "G2" in kamp else "G1",
        }
    return sonuc

def sb_indir(ad):
    url = f"{SB_URL}/storage/v1/object/public/{BUCKET}/{ad}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[!] Supabase'den {ad} okunamadi ({e}); repodaki kopya kullanilacak")
        with open(ad, encoding="utf-8") as f: return json.load(f)

def sb_yukle(ad, veri):
    gövde = json.dumps(veri, ensure_ascii=False, indent=1).encode()
    req = urllib.request.Request(
        f"{SB_URL}/storage/v1/object/{BUCKET}/{ad}", data=gövde, method="POST",
        headers={"Authorization":f"Bearer {SB_KEY}", "apikey":SB_KEY,
                 "Content-Type":"application/json", "x-upsert":"true",
                 "Cache-Control":"max-age=60"})
    with urllib.request.urlopen(req, timeout=120) as r:
        print(f"[+] Supabase'e yuklendi: {ad} ({r.status})")

def main():
    veri = sb_indir("data.json")
    baslangic = veri.get("test",{}).get("yayin") or "2026-08-17"
    bitis = datetime.date.today().isoformat()
    print(f"[>] Meta araligi: {baslangic} -> {bitis} · hesap {ACCOUNT} · kampanya oneki '{KAMPANYA_ONEK}'")

    m = meta_verisi(baslangic, bitis)
    if not m:
        print("[!] Meta'dan eslesen reklam seti gelmedi. Kampanya adi oneki ve hesap kimligini kontrol et.")
        return 1

    degisen = 0
    for satir in veri.get("testRows", []):
        d = m.get(satir["kod"])
        if not d: continue
        g = d["gosterim"]
        satir["harcama"]  = round(d["harcama"], 2)
        satir["gosterim"] = int(g)
        satir["hook"]     = round(d["hook_n"]/g, 4) if g else None
        satir["hold"]     = round(d["hold_n"]/g, 4) if g else None
        satir["lead"]     = int(d["lead"])
        satir["asama"]    = d["asama"]
        satir["tarih"]    = satir.get("tarih") or baslangic
        degisen += 1
        # randevu / islem CRM alani -> dokunulmuyor

    veri.setdefault("meta", {})["metaSenkron"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[+] {degisen}/{len(veri.get('testRows',[]))} video guncellendi")
    sb_yukle("data.json", veri)
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=1)
    return 0

if __name__ == "__main__":
    sys.exit(main())
