# StickStory — otomatik çöp adam komedi kanalı

Tasarım dokümanı: `cizgi-hikaye-kanali.md`. Her şey GitHub Actions üzerinde çalışır (`.github/workflows/videos.yml`).

```
senaryo (Gemini yazar + Gemini hakem ≥7, yoksa bank/) → Kokoro TTS (karakter başına ses)
→ cairo vektör animasyon (rig, 15 sahne, kamera, lip-sync, altyazı, efekt) → prosedürel müzik/SFX → ffmpeg → YouTube
```

Takvim: Shorts TR 16:00 / 20:00 / 01:00 (günde 3), uzun video Cumartesi 20:00.

## Kurulum (bir kez)
1. Repo secrets: `GEMINI_API_KEY` (diğer kanallarındaki anahtarla aynı olabilir)
2. YouTube kanalını bağla (tarayıcıda kanal seçimi):
   `python auth_setup.py --repo <kullanıcı>/stickstory` → `YT_CLIENT_ID/SECRET/REFRESH_TOKEN/CHANNEL_ID` secrets'a yazılır
3. Repo variable: `YT_PRIVACY=public` (ilk günler `private` ile kontrol edebilirsin)

## Yerel test
```
pip install -r requirements.txt
python run.py --script bank/001_birthday_cake.json --no-upload   # output/<id>/video.mp4
python run.py --format long --no-upload                           # uzun video (Gemini gerekir)
```
