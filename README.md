# 女郎花

女郎は遊女の事ではありません。万葉の時代には自然の中にある静かな美しさが
好まれました。女郎は美しい女の人という意味で、貴族女性に対する誉め言葉でした。
最近の人は遊女だと思っていることが多いので、ここに記しておきます。

**女郎花（Ominaeshi）** は Marvelous Designer（MD）から受け取った服 OBJ と
Pattern JSON を、Blender 内の **HOU衣服コレクション**へ変換する拡張です。
元のOBJは変更しません。表示とメッセージは日本語です。

## パイプライン（v0.3.2）

```text
Blender ボディ ──(frame 1 ABC)──► MDで服作成 ──(OBJ + Pattern JSON)──► 女郎花
                                                                           │
                                                              HOUコレクション
                                                                           │
                                                                        KOROMO
```

HOUには次を保存します。

- 面でつながった服パーツごとのワールド座標メッシュ
- メートル単位の平面型紙座標 `housei_pattern_position`
- 各パーツの `HOU` JSON（`housei-hou/1.0.0`）
- MDの縫い線から求めた正確な頂点ペア
  `housei_sewing_plan_json`（`housei-sewing-plan/1.0.0`）
- 元のPattern JSON（Blender Textデータ）

自己衝突の検査・修理やシミュレーションは行いません。シミュレーションは
HOU対応のKOROMOなど、下流のソルバーが担当します。

縫い頂点ペアは、Pattern JSONが指定するパーツ組の中から、MDの縫製後OBJで
同じ位置に重なっている別境界頂点を直接検出します。そのため、MDで縫製
シミュレーションを済ませてから服OBJを送信してください。

## 使い方

1. Nパネルの **女郎花** を開き、**MDブリッジ開始**を押します。
2. MDのPlug-in Managerで同梱の `md_addon/1_get_BL_avater.py` と
   `md_addon/2_send_clothes_BL.py` を登録します。
3. MDで `1_get_BL_avater` → 服作成 → `2_send_clothes_BL` を実行します。
4. 女郎花の「服 OBJ」に取り込んだ服が設定されていることを確認します。
5. **HOU** ボタンを押します。
6. 出力された `*_HOU` コレクションをKOROMOの「HOUコレクション」に指定します。

MD側プラグインは全フレームABC、服ABC、ヘアーABCを使いません。一時ファイルは
`%TEMP%\tanabata`、ログはMDが `~/ominaeshi_md.log`、Blenderが
`~/ominaeshi_md_bridge.log` です。

## 要件

- Blender 5.2以降（Windows x64）
- Marvelous Designer
- 同梱MDプラグインの1と2

外部接触ソルバー、OpenMP、交差検査DLLは不要です。

## ビルド

```powershell
& "C:\Users\azoo\git\build_windows_Release_x64_vc17_Release\bin\blender.exe" `
  --command extension build --source-dir . --output-dir .\dist
```

成果物: `dist/ominaeshi-0.3.2.zip`

## ライセンス

GPL-3.0-or-later。MDブリッジの由来は `THIRD_PARTY_NOTICES.md` を参照してください。
