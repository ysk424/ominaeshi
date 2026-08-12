# 女郎花

女郎は遊女の事ではありません。万葉の時代には自然の中にある静かな美しさが好まれました。女郎は美しい女の人という意味で、貴族女性に対する誉め言葉でした。最近の人は遊女だとおもっていることが多いので、ここに記しておきます。

**Marvelous Designer (MD)** と **Blender** のあいだでボディ／服を往復し、縫い辺を作り直し、
自己交差を検査・修理したうえで **ZOZO Contact Solver** にセットする Blender 拡張です。

表示とメッセージは**日本語のみ**（私用）。

## パイプライン（v0.2.0）

```
Blender ボディ ──(frame1 ABC)──► MD で服作成 ──(OBJ+縫いメタ)──► Blender
                                                                      │
                                                              ZOZO用準備 → ZOZO
```

1. 女郎花パネルで **ボディ** を指定し **MD ブリッジ開始**
2. MD: **1_get_BL_avater**（静的ボディを受け取る）
3. MD で服を作成
4. MD: **2_send_clothes_BL**（服 OBJ を Blender へ）
5. 女郎花: **ZOZO用準備** → Transfer / Run Simulation

MD 側プラグインは **tanabata の既存 1 / 2 をそのまま**使います（コピー不要）。  
**使わない:** 3（全フレームボディ）、4（服 ABC）、ヘアー ABC。  
**一時ファイル:** 常に `%TEMP%\tanabata`（storage 設定 UI なし）。

## 使い方

1. N パネル **女郎花** を開く。
2. **ボディ** をセット（未設定なら **自動セット**）。
3. **MD ブリッジ → 開始**（`:7422`）。Tanabata リスナーと同時起動は不可。
4. MD で 1 → 服作成 → 2。服は自動で「服」欄に入ります。
5. 必要なら **足首 / 首**（cm）を調整。
6. **ZOZO用準備** を押す。

### ZOZO用準備の内容

1. 服を ZOZO 用コレクションへコピー（元オブジェクトは変更しない）
2. **ZOZO 向け縫い辺（緩いステッチ）を再構築**（パネル間＋同一パネルの筒閉じ・ダーツ）
3. ボディをコピーし、**足首より下・首より上をカット**（アーマチュア親・修飾子は残し、アニメ可）
4. 自己交差 **検査 → 修理 → 再検査**（shell-isect）
5. 三角の最小面積など品質チェック
6. **PASS** なら ZOZO MCP を縫製と同じパラメータで起動・設定
7. **NG** ならメッセージ欄に理由を出して止める

## 要件

- Blender 5.2+（Windows x64）
- Marvelous Designer（tanabata の MD プラグイン 1 / 2）
- ZOZO Contact Solver 拡張（MCP）
- 同梱 `bin/shell_isect.dll`

## ビルド

```powershell
& "C:\Users\azoo\git\build_windows_Release_x64_vc17_Release\bin\blender.exe" `
  --command extension build --source-dir . --output-dir .\dist
```

成果物: `dist/ominaeshi-0.2.0.zip`

## ライセンス

GPL-3.0-or-later。第三者については `THIRD_PARTY_NOTICES.md` を参照。
