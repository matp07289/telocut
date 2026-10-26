# TeloCut v0.32.1 paid（parts ダウンロード）

GitHub は 1 ファイル 100MB を越える push を受け付けないため、
144.59MB の zip を 70MB の parts に分割してあります。
.001, .002 の順に結合すると元の zip になります。

## 結合（Windows cmd）
```
copy /b TeloCut_v0.32.1_paid.zip.001+TeloCut_v0.32.1_paid.zip.002 TeloCut_v0.32.1_paid.zip
```
## 結合（PowerShell）
```
$parts = Get-ChildItem 'TeloCut_v0.32.1_paid.zip.*' | Sort-Object Name
$out = [System.IO.File]::Create('TeloCut_vpaid.zip')
foreach ($p in $parts) { $in = [System.IO.File]::OpenRead($p.FullName); $in.CopyTo($out); $in.Close() }
$out.Close()
```
## 検証
結合した zip の SHA256 が下の値と一致するか確認してください。
```
certutil -hashfile TeloCut_v0.32.1_paid.zip SHA256
```

- 元の zip の SHA256: `cad7c7eedcf9fae01c2a78a2a655bd171d930467f7a95a483355013cdbc32348`
- `TeloCut_v0.32.1_paid.zip` = 144.59MB, 3 parts

| part | size | SHA256 |
|---|---|---|
| TeloCut_v0.32.1_paid.zip.001 | 70.00MB | `51c6e4b2cac7a9710849a8802dadbdc3d0ca27678c99cf70c578db921811ff6e` |
| TeloCut_v0.32.1_paid.zip.002 | 70.00MB | `866e60baff9f5ad9431bb0b482bfbde5ae07c946128888999fb6b929d89707f4` |
| TeloCut_v0.32.1_paid.zip.003 | 4.59MB | `6f560b8e3ae8a30658858a230937f361c62cf99eb75049d983a72c778772e62c` |

中のアプリ: TeloCut.exe / TeloCutCLI.exe / TeloCutAssets.exe / DavinTelop.exe。
解凍して TeloCut.exe を起動してください（インストール不要、PC に置いたまま使う）。
有料版（購入者用）。キーで Pro / Studio、キーがなければ無料版＋Pro の 14 日間お試し。
