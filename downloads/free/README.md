# TeloCut v0.32.1 free（parts ダウンロード）

GitHub は 1 ファイル 100MB を越える push を受け付けないため、144.59MB の zip を
3 つの parts（70MB + 70MB + 4.59MB）に分割してあります。
.001, .002, .003 の順に結合すると元の zip になります。

## 結合（Windows cmd）
```
copy /b TeloCut_v0.32.1_free.zip.001+TeloCut_v0.32.1_free.zip.002+TeloCut_v0.32.1_free.zip.003 TeloCut_v0.32.1_free.zip
```
## 結合（PowerShell）
```
$parts = Get-ChildItem 'TeloCut_v0.32.1_free.zip.0*' | Sort-Object Name
$out = [System.IO.File]::Create('TeloCut_v0.32.1_free.zip')
foreach ($p in $parts) { $in = [System.IO.File]::OpenRead($p.FullName); $in.CopyTo($out); $in.Close() }
$out.Close()
```
## 検証
結合した zip の SHA256 が下の値と一致するか確認してください。
```
certutil -hashfile TeloCut_v0.32.1_free.zip SHA256
```

- 元の zip の SHA256: `dd44e544102da2b077c7c3e3a2bffe932e2c81ca69929b2789ce844d3273989e`

| part | size | SHA256 |
|---|---|---|
| TeloCut_v0.32.1_free.zip.001 | 70.00MB | `d1316ce826c1b910c70146d9669bc5c5b9205a79ae1622d0931018a0c75a1f49` |
| TeloCut_v0.32.1_free.zip.002 | 70.00MB | `2062c6a69ef94fedb6dd16a649866c1e244695b91b38252dc7df4e3cebe24140` |
| TeloCut_v0.32.1_free.zip.003 | 4.59MB | `96448d719f38a49906d0770e8c4e7696e4ce4ca1a1eae0e7e0674bfe0ebab5f0` |

中のアプリ: TeloCut.exe / TeloCutCLI.exe / TeloCutAssets.exe / DavinTelop.exe。
解凍して TeloCut.exe を起動してください（インストール不要、PC に置いたまま使う）。
無料版（だれにでも配れる）。書き出しに透かしが入ります。
