#!/usr/bin/env python3
"""
Salesforce -> data.json -> Supabase Storage senkronizasyonu.

Meta senkronundan SONRA calisir; randevu ve islem sutunlarini CRM'den doldurur.
Meta'nin doldurdugu alanlara (harcama, gosterim, hook, hold, lead) dokunmaz.

Ortam degiskenleri (GitHub Secrets):
  SF_DOMAIN            ornek: rithrales.my.salesforce.com   (https:// yazma)
  SF_CLIENT_ID         Connected App Consumer Key
  SF_CLIENT_SECRET     Connected App Consumer Secret
  SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET

Ayarlanabilir (GitHub Variables) — varsayilanlar parantezde:
  SF_OBJECT            (Lead)              hangi nesne
  SF_KOD_FIELD         (Video_Kodu__c)     video kodunu tutan alan
  SF_RANDEVU_KOSUL     (Randevu__c = true) randevu sayilma kosulu
  SF_ISLEM_KOSUL       (Islem__c = true)   islem sayilma kosulu
"""
import os, re, sys, json, datetime, urllib.parse, urllib.request
def normalize(s):
    """Reklam adlarini karsilastirilabilir hale getirir:
    Turkce harf, buyuk/kucuk, bosluk/alt cizgi/tire farklarini siler."""
    if not s: return ""
    t = str(s)
    for a,b in [("İ","I"),("ı","i"),("Ş","S"),("ş","s"),("Ğ","G"),("ğ","g"),
                ("Ç","C"),("ç","c"),("Ö","O"),("ö","o"),("Ü","U"),("ü","u")]:
        t = t.replace(a,b)
    t = t.upper()
    return re.sub(r"[^A-Z0-9]", "", t)


API_V = "v62.0"
DOMAIN = os.environ["SF_DOMAIN"].replace("https://", "").rstrip("/")
CID    = os.environ["SF_CLIENT_ID"]
CSEC   = os.environ["SF_CLIENT_SECRET"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = os.environ.get("SUPABASE_BUCKET", "panel")

OBJ      = os.environ.get("SF_OBJECT", "Account")
# Eslestirme alani: CRM'e Meta'dan otomatik gelen REKLAM ADI alani.
# Video kodu alani varsa SF_KOD_FIELD olarak ayrica verilebilir.
KOD      = os.environ.get("SF_REKLAM_FIELD") or os.environ.get("SF_KOD_FIELD", "CR_AdName__c")
ASAMA    = os.environ.get("SF_ASAMA_FIELD", "Stage__c")
KAMPANYA = os.environ.get("SF_KAMPANYA_FIELD", "CR_CampaignName__c")
KAMP_KOSUL = os.environ.get("SF_KAMPANYA_KOSUL", "")   # orn: CR_CampaignName__c LIKE 'Video Test%'

def liste(ad, varsayilan):
    ham = os.environ.get(ad, "")
    return [x.strip() for x in ham.split("|") if x.strip()] or varsayilan

# Randevu KUMULATIF: randevu verilmis her kayit (iptal ve ilerlemis olanlar dahil).
# G2 karari "kac randevu uretti" sorusuna dayaniyor, "su an kac randevu bekliyor" degil.
ASAMA_RANDEVU = liste("SF_ASAMA_RANDEVU",
    ["Randevu Verildi","Randevu İptal Edildi","Hasta İşlem Oldu","Hasta Muayene oldu"])
ASAMA_MUAYENE = liste("SF_ASAMA_MUAYENE", ["Hasta Muayene oldu"])
ASAMA_ISLEM   = liste("SF_ASAMA_ISLEM",   ["Hasta İşlem Oldu"])

def kosul(degerler):
    icerik = ",".join("'" + d.replace("'", r"\'") + "'" for d in degerler)
    return f"{ASAMA} IN ({icerik})"

def _govde(e):
    try: return e.read().decode()
    except Exception: return ""

def jetonu_al():
    veri = urllib.parse.urlencode({
        "grant_type":"client_credentials","client_id":CID,"client_secret":CSEC}).encode()
    url = f"https://{DOMAIN}/services/oauth2/token"
    req = urllib.request.Request(url, data=veri,
        headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        g = _govde(e)
        print(f"[X] Salesforce jeton hatasi (HTTP {e.code}) · adres: {url}")
        print(f"    Yanit: {g or '(bos)'}")
        print("    invalid_client   -> Consumer Key/Secret yanlis, ya da uygulama henuz yayilmadi (10 dk bekle)")
        print("    invalid_grant    -> Client Credentials akisinda 'Run As' kullanicisi atanmamis")
        print("    inactive_org / bulunamadi -> SF_DOMAIN yanlis. 'xxx.my.salesforce.com' olmali,")
        print("                                 'lightning.force.com' DEGIL.")
        raise SystemExit(1)
    return d["access_token"], d.get("instance_url", f"https://{DOMAIN}")

def soql(jeton, temel, sorgu):
    url = f"{temel}/services/data/{API_V}/query?q=" + urllib.parse.quote(sorgu)
    kayitlar = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {jeton}"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            print(f"[X] SOQL hatasi (HTTP {e.code}): {_govde(e)[:500]}")
            print(f"    Sorgu: {sorgu}")
            raise SystemExit(1)
        kayitlar += d.get("records", [])
        url = (temel + d["nextRecordsUrl"]) if not d.get("done") and d.get("nextRecordsUrl") else None
    return kayitlar

def say(jeton, temel, kosul=None):
    """Video koduna gore adet dondurur."""
    nerede = f"{KOD} != null"
    if KAMP_KOSUL: nerede += f" AND ({KAMP_KOSUL})"
    if kosul:      nerede += f" AND ({kosul})"
    q = f"SELECT {KOD}, COUNT(Id) adet FROM {OBJ} WHERE {nerede} GROUP BY {KOD}"
    out = {}
    for r in soql(jeton, temel, q):
        kod = r.get(KOD)
        adet = r.get("adet", r.get("expr0", 0))
        if kod: out[normalize(kod)] = int(adet or 0)
    return out

def veri_oku():
    if os.path.exists("data.json"):
        with open("data.json", encoding="utf-8") as f: return json.load(f)
    url = f"{SB_URL}/storage/v1/object/public/{BUCKET}/data.json"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())

def sb_yukle(ad, veri):
    gövde = json.dumps(veri, ensure_ascii=False, indent=1).encode()
    req = urllib.request.Request(f"{SB_URL}/storage/v1/object/{BUCKET}/{ad}",
        data=gövde, method="POST",
        headers={"Authorization":f"Bearer {SB_KEY}","apikey":SB_KEY,
                 "Content-Type":"application/json","x-upsert":"true",
                 "Cache-Control":"max-age=60"})
    with urllib.request.urlopen(req, timeout=120) as r:
        print(f"[+] Supabase'e yuklendi: {ad} ({r.status})")

def main():
    jeton, temel = jetonu_al()
    print(f"[>] Salesforce baglandi · nesne {OBJ} · eslestirme {KOD} · asama {ASAMA}")
    if KAMP_KOSUL: print(f"[>] Kampanya filtresi: {KAMP_KOSUL}")

    crm_lead = say(jeton, temel)
    randevu  = say(jeton, temel, kosul(ASAMA_RANDEVU))
    muayene  = say(jeton, temel, kosul(ASAMA_MUAYENE))
    islem    = say(jeton, temel, kosul(ASAMA_ISLEM))
    print(f"[>] CRM: {len(crm_lead)} reklam · {sum(crm_lead.values())} kayit · "
          f"{sum(randevu.values())} randevu · {sum(muayene.values())} muayene · {sum(islem.values())} islem")

    veri = veri_oku()
    degisen = 0
    for satir in veri.get("testRows", []):
        k = next((x for x in (normalize(satir.get("dosya","")),
                              normalize(satir.get("slug","")),
                              normalize(satir["kod"]))
                  if x in crm_lead or x in randevu or x in islem), None)
        if k is None: continue
        satir["randevu"] = randevu.get(k, 0)
        satir["muayene"] = muayene.get(k, 0)
        satir["islem"]   = islem.get(k, 0)
        satir["crmLead"] = crm_lead.get(k, 0)
        degisen += 1

    veri.setdefault("meta", {})["crmSenkron"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[+] {degisen}/{len(veri.get('testRows',[]))} video guncellendi")
    sb_yukle("data.json", veri)
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=1)
    return 0

if __name__ == "__main__":
    sys.exit(main())
