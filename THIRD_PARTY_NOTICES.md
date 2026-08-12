# 第三者表記

## Tanabata（MD ブリッジ由来）

MD 往復（TCP リスナー、ボディ frame1 ABC、服 OBJ 取込、Ageha 縫いメタ）は
tanabata の設計・プロトコルに合わせて再実装しています。MD 側プラグインは
tanabata 同梱のものをそのまま使います。

## ZOZO Contact Solver (ppf-contact-solver)

女郎花は ZOZO Contact Solver をローカル MCP 経由で設定します。ソルバ本体は
同梱しません。Apache License 2.0 で公開されています。

<https://github.com/st-tech/ppf-contact-solver>

## shell-isect

同梱 `bin/shell_isect.dll` は三角メッシュ自己交差ライブラリです（Apache-2.0）。

<https://github.com/ysk424/shell-isect>

## Microsoft Visual C++ OpenMP Runtime

Windows x64 パッケージに `bin/vcomp140.dll` を同梱します。Visual Studio の
再頒布可能パッケージ由来であり、本プロジェクトの GPL 対象外です。

<https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files>
