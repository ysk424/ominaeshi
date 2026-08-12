# 女郎花

女郎は遊女の事ではありません。万葉の時代には自然の中にある静かな美しさが好まれました。女郎は美しい女の人という意味で、貴族女性に対する誉め言葉でした。最近の人は遊女だとおもっていることが多いので、ここに記しておきます。

マーベラスデザイナーなどで作った**服メッシュ**と**ボディ**を選び、ZOZO 向けに
縫い辺を作り直し、自己交差を検査・修理したうえで **ZOZO Contact Solver** に
セットする Blender 拡張です。

縫製 (housei) の後半（ZOZO用準備・MCP 設定）を、型紙・HOU なしの MD 服向けに
切り出したものです。表示とメッセージは**日本語のみ**（私用）。

## 使い方

1. N パネル **女郎花** を開く（未設定なら `output.001` と `CC_Base_Body` を自動セット）。
2. 必要なら **自動セット** で再取得。
3. **足首 / 首**（cm）でボディの書き出し高さを調整（自動推定あり）。
4. **ZOZO用準備** を押す。

処理内容:

1. 服を ZOZO 用コレクションへコピー（元オブジェクトは変更しない）
2. **ZOZO 向け縫い辺（緩いステッチ）を再構築**（パネル間の境界最近傍＋同一パネルの筒閉じ・ダーツ）
3. ボディをコピーし、**足首より下・首より上をカット**（アーマチュア親・修飾子は残し、アニメ可）
4. 自己交差 **検査 → 修理 → 再検査**（shell-isect）
5. 三角の最小面積など品質チェック
6. **PASS** なら ZOZO MCP を縫製と同じパラメータで起動・設定
7. **NG** ならメッセージ欄に理由を出して止める

## 要件

- Blender 5.2+（Windows x64）
- ZOZO Contact Solver 拡張（MCP）
- 同梱 `bin/shell_isect.dll`

## ビルド

```powershell
& "C:\Users\azoo\git\build_windows_Release_x64_vc17_Release\bin\blender.exe" `
  --command extension build --source-dir . --output-dir .\dist
```

成果物: `dist/ominaeshi-0.1.1.zip`

## ライセンス

GPL-3.0-or-later。第三者については `THIRD_PARTY_NOTICES.md` を参照。
