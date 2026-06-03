# DavinTelop

**DaVinci Resolve / Premiere Pro 向け テロップ作成ツール**

CSV読み込みまたは直接入力でテロップデータを管理し、透過PNG・SRT・FCPxml の書き出し、DaVinci Resolve への直接配置まで対応した Windows 用デスクトップアプリです。

---

## 動作環境

| 項目 | 要件 |
|---|---|
| OS | Windows 10 / 11 (64bit) |
| .NET / ランタイム | 不要（スタンドアロン EXE） |
| DaVinci Resolve | 18 以降（API連携使用時） |
| Premiere Pro | CC 2020 以降（合成プレビュー使用時） |

---

## インストール

1. `DavinTelop_vX.X.X.zip` を任意のフォルダに解凍
2. `DavinTelop.exe` をダブルクリックして起動

インストール作業は不要です。レジストリへの書き込みも行いません。

---

## 主な機能

| 機能 | 概要 |
|---|---|
| CSVインポート | start / end / text 列を持つ CSV を読み込み |
| 直接入力 | タイムコード + テキストをその場で入力して追加 |
| デザイン設定 | フォント・縁取り・シャドウ・背景・位置を GUI で調整 |
| リアルタイムプレビュー | 設定変更が即座にプレビューに反映 |
| PNG 書き出し | テロップ1枚ずつ透過 PNG として書き出し |
| SRT 書き出し | 字幕ファイル（SRT形式）として書き出し |
| FCPxml 書き出し | Final Cut Pro XML でタイムライン情報を書き出し |
| Resolve スクリプト生成 | DaVinci Resolve コンソール用 Python スクリプトを生成 |
| Resolve API 送信 | DaVinci Resolve が起動中であれば直接タイムラインに配置 |
| 合成プレビュー | Premiere Pro / Resolve の映像に Photoshop PNG を重ねて確認 |
| プリセット管理 | デザイン設定をプリセットとして保存・呼び出し |

---

## クイックスタート

### テロップを作って Resolve に配置する（基本フロー）

1. DaVinci Resolve を起動し、プロジェクト・タイムラインを開いておく
2. DavinTelop を起動
3. 「直接入力」エリアで開始TC・テキストを入力 → **追加** ボタン
4. デザインパネルでフォント・スタイルを調整
5. 下部バーの「出力先」フォルダを指定
6. **⚡ Resolve API送信** ボタンをクリック

### Resolve API が使えない場合

1. 上記手順 1〜5 を実施
2. **🐍 Resolveスクリプト生成** ボタンをクリック
3. Resolve で `Workspace > Console` を開く
4. 生成された `davintelop_resolve.py` をコンソールにドラッグ＆ドロップ
5. Enter キーで実行

---

## DaVinci Resolve API の有効化

Resolve 側で外部スクリプトを許可する設定が必要です。

`DaVinci Resolve > Preferences > General`
→ **External scripting using: Local** に設定
→ Resolve を再起動

---

## Photoshop 連携（合成プレビュー）

詳細は同梱の **DavinTelop_マニュアル.pdf** を参照してください。

---

## アンインストール

解凍したフォルダを削除するだけです。
設定ファイルが `C:\Users\ユーザー名\.davintelop\` に保存されますので、
完全に削除する場合はこのフォルダも削除してください。

---

## ライセンス

MIT License — 個人・商用利用ともに自由にご使用いただけます。

---

## 更新履歴

### v1.0.0
- 初回リリース
