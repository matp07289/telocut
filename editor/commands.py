"""コマンド一覧とキーボード配列プリセット。

Avid Media Composer の「コマンドパレット」に相当。各コマンドは id で識別し、
キー割り当て（keymap: id -> [キー, ...]）はプリセットから始めてユーザーが自由に変更できる。
キー表記は QKeySequence の PortableText（例 "Ctrl+Shift+F", "Space", ",", "F9"）。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List

# (id, 表示名, カテゴリ, 短い表示（キーボード上のラベル）)
COMMANDS = [
    # ── 再生・移動（Avid: Move / Play）──
    ("play_stop", "再生 / 停止", "再生・移動", "▶■"),
    ("shuttle_j", "逆再生（J・倍速）", "再生・移動", "◀◀"),
    ("shuttle_k", "停止（K）", "再生・移動", "■"),
    ("shuttle_l", "再生（L・倍速）", "再生・移動", "▶▶"),
    ("play_in_out", "IN から OUT を再生", "再生・移動", "▶{}"),
    ("step_back_1", "1フレーム戻る", "再生・移動", "◀1"),
    ("step_fwd_1", "1フレーム進む", "再生・移動", "1▶"),
    ("step_back_10", "10フレーム戻る", "再生・移動", "◀10"),
    ("step_fwd_10", "10フレーム進む", "再生・移動", "10▶"),
    ("step_back_5", "5フレーム戻る", "再生・移動", "◀5"),
    ("step_fwd_5", "5フレーム進む", "再生・移動", "5▶"),
    ("prev_edit", "前の編集点へ", "再生・移動", "|◀"),
    ("next_edit", "次の編集点へ", "再生・移動", "▶|"),
    ("goto_start", "先頭へ", "再生・移動", "⇤"),
    ("goto_end", "末尾へ", "再生・移動", "⇥"),
    ("goto_in", "IN 点へ移動", "再生・移動", "→{"),
    ("goto_out", "OUT 点へ移動", "再生・移動", "→}"),
    ("goto_tc", "タイムコードへ移動", "再生・移動", "TC"),
    ("toggle_monitor", "ソース / レコード 切替", "再生・移動", "S⇄R"),
    ("fullscreen", "フルスクリーン再生", "再生・移動", "全画"),
    ("loop_toggle", "ループ再生 切替", "再生・移動", "⟲"),
    # ── マーク ──
    ("mark_in", "IN 点を設定", "マーク", "{"),
    ("mark_out", "OUT 点を設定", "マーク", "}"),
    ("mark_clip", "クリップに IN/OUT（Mark Clip）", "マーク", "{▭}"),
    ("clear_in", "IN 点を消去", "マーク", "✕{"),
    ("clear_out", "OUT 点を消去", "マーク", "✕}"),
    ("clear_both", "IN/OUT を消去", "マーク", "✕{}"),
    ("add_marker", "マーカーを追加", "マーク", "◆"),
    # ── 編集（Avid: Edit）──
    ("splice_in", "スプライスイン（挿入）", "編集", "挿入"),
    ("overwrite", "上書き", "編集", "上書"),
    ("lift", "リフト", "編集", "リフト"),
    ("extract", "抽出", "編集", "抽出"),
    ("copy_clipboard", "クリップボードにコピー（IN-OUT / 選択）", "編集", "Copy"),
    ("paste", "貼り付け（再生ヘッド）", "編集", "Paste"),
    ("cut", "切り取り", "編集", "Cut"),
    ("add_edit", "アドエディット（再生ヘッドで分割）", "編集", "分割"),
    ("add_edit_all", "アドエディット（全トラック）", "編集", "分割全"),
    ("delete", "削除（セグメント）", "編集", "削除"),
    ("ripple_delete", "リップル削除", "編集", "R削除"),
    ("ripple_trim_prev", "前の編集点までリップルトリム", "編集", "Q"),
    ("ripple_trim_next", "次の編集点までリップルトリム", "編集", "W"),
    ("match_frame", "マッチフレーム", "編集", "Match"),
    ("toggle_enabled", "クリップの有効 / 無効", "編集", "有効"),
    ("toggle_link", "リンク / リンク解除", "編集", "Link"),
    ("add_fades", "フェードイン/アウトを追加", "編集", "Fade"),
    ("add_telop", "再生ヘッドにテロップを追加", "編集", "T+"),
    ("add_dissolve", "クロスディゾルブを追加", "編集", "Dslv"),
    ("speed", "速度を変更...", "編集", "速度"),
    ("freeze_frame", "フリーズフレームを挿入（2秒）", "編集", "Frz"),
    ("paste_attributes", "属性をペースト...", "編集", "属性"),
    ("replace_edit", "置き換え編集（ソースで差し替え）", "編集", "Repl"),
    ("close_gaps", "選択トラックの隙間をすべて詰める", "編集", "詰め"),
    ("select_forward", "再生ヘッドより後ろをすべて選択", "編集", "→選"),
    ("subclip", "サブクリップを作成（ソースの IN/OUT）", "編集", "Sub"),
    ("find_bin", "ビンで素材を表示（Find Bin）", "編集", "Find"),
    ("project_find", "プロジェクト内を検索", "編集", "検索"),
    ("undo", "元に戻す", "編集", "Undo"),
    ("redo", "やり直し", "編集", "Redo"),
    ("select_all_clips", "すべてのクリップを選択", "編集", "全選"),
    ("deselect_all", "選択解除", "編集", "解除"),
    # ── オーディオ・解析 ──
    ("normalize", "ノーマライズ（ピーク −1dB）", "オーディオ", "Norm"),
    ("audio_crossfades", "編集点にオーディオクロスフェード", "オーディオ", "AXF"),
    ("silence_cut", "無音カット（ジェットカット）...", "オーディオ", "無音"),
    ("scene_detect", "シーンチェンジ検出...", "オーディオ", "Scene"),
    # ── 読み込み・書き出し ──
    ("import_interchange", "他ソフトから読み込み（AAF / Premiere / XML / EDL）", "書き出し", "取込"),
    ("export_aaf", "AAF を書き出し（Avid / Premiere）", "書き出し", "AAF"),
    ("export_xml", "Final Cut Pro XML を書き出し（Premiere / Resolve）", "書き出し", "XML"),
    ("export_edl", "EDL（CMX3600）を書き出し", "書き出し", "EDL"),
    ("export_frame", "フレームを静止画で書き出し", "書き出し", "PNG"),
    ("export_wav", "音声のみ書き出し（WAV）", "書き出し", "WAV"),
    ("export_chapters", "マーカーを YouTube チャプターで書き出し", "書き出し", "Chap"),
    ("export_queue", "書き出しキュー（複数シーケンス）", "書き出し", "Queue"),
    # ── トラック ──
    ("select_all_tracks", "全トラックを選択", "トラック", "全Trk"),
    ("deselect_all_tracks", "全トラックの選択を解除", "トラック", "Trk解"),
    ("toggle_v1", "V1 を選択 / 解除", "トラック", "V1"),
    ("toggle_v2", "V2 を選択 / 解除", "トラック", "V2"),
    ("toggle_v3", "V3 を選択 / 解除", "トラック", "V3"),
    ("toggle_v4", "V4 を選択 / 解除", "トラック", "V4"),
    ("toggle_a1", "A1 を選択 / 解除", "トラック", "A1"),
    ("toggle_a2", "A2 を選択 / 解除", "トラック", "A2"),
    ("toggle_a3", "A3 を選択 / 解除", "トラック", "A3"),
    ("toggle_a4", "A4 を選択 / 解除", "トラック", "A4"),
    # ── トリム（Avid: Trim）──
    ("trim_mode", "トリムモード 入/切", "トリム", "Trim"),
    ("trim_m10", "トリム −10 フレーム", "トリム", "−10"),
    ("trim_m1", "トリム −1 フレーム", "トリム", "−1"),
    ("trim_p1", "トリム +1 フレーム", "トリム", "+1"),
    ("trim_p10", "トリム +10 フレーム", "トリム", "+10"),
    ("trim_side_a", "トリム A 側（出側）", "トリム", "A"),
    ("trim_side_ab", "トリム 両側（ローリング）", "トリム", "AB"),
    ("trim_side_b", "トリム B 側（入側）", "トリム", "B"),
    # ── ツール（Avid: Smart Tool / Premiere: ツール）──
    ("tool_select", "赤セグメント（リフト/上書き）・選択ツール", "ツール", "▲赤"),
    ("tool_yellow", "黄セグメント（抽出/スプライスイン）", "ツール", "▲黄"),
    ("tool_ripple", "リップルトリム（黄ローラー）", "ツール", "R黄"),
    ("tool_roll", "ローリング編集", "ツール", "Roll"),
    ("tool_slip", "スリップ", "ツール", "Slip"),
    ("tool_slide", "スライド", "ツール", "Slid"),
    ("tool_razor", "レーザー", "ツール", "✂"),
    ("tool_hand", "手のひら", "ツール", "✋"),
    ("tool_zoom", "ズーム", "ツール", "⊕"),
    ("snap_toggle", "スナップ 切替", "ツール", "Snap"),
    ("zoom_in", "タイムライン拡大", "ツール", "＋"),
    ("zoom_out", "タイムライン縮小", "ツール", "－"),
    ("zoom_fit", "シーケンス全体を表示", "ツール", "全体"),
    ("focus", "フォーカス（再生ヘッド周辺を拡大/戻す）", "ツール", "Focus"),
    # ── レンダリング ──
    ("render_in_out", "IN-OUT をレンダリング", "レンダリング", "Rend"),
    ("render_all", "要レンダリング部分をすべてレンダリング", "レンダリング", "R全"),
    ("render_cancel", "レンダリングを中止", "レンダリング", "R中"),
    ("proxy_selected", "選択素材のプロキシを作成", "レンダリング", "Proxy"),
    ("toggle_proxy", "プロキシ再生 切替", "レンダリング", "Px"),
    # ── ファイル・ウィンドウ ──
    ("new_bin", "新規ビン", "ファイル", "Bin+"),
    ("new_sequence", "新規シーケンス", "ファイル", "Seq+"),
    ("close_project", "プロジェクトを閉じる（プロジェクトの選択へ）", "ファイル", "閉じ"),
    ("new_project", "新規プロジェクト", "ファイル", "新規"),
    ("open_project", "プロジェクトを開く", "ファイル", "開く"),
    ("save_project", "保存", "ファイル", "保存"),
    ("save_as", "名前を付けて保存", "ファイル", "別名"),
    ("import", "素材を読み込み", "ファイル", "読込"),
    ("export", "メディアを書き出し", "ファイル", "書出"),
    ("keyboard_settings", "キーボード設定", "ファイル", "Key"),
    ("preferences", "環境設定", "ファイル", "設定"),
    ("workspace_single", "ワークスペース: シングルディスプレイ", "ウィンドウ", "WS1"),
    ("workspace_dual", "ワークスペース: マルチディスプレイ（推奨配置）", "ウィンドウ", "WS2"),
    ("shortcuts_help", "ショートカット一覧", "ウィンドウ", "?"),
    ("tc_window", "大型タイムコード表示", "ウィンドウ", "TC"),
    # ── YouTube・ゆっくり ──
    ("voice_panel", "セリフパネル（1 行ずつ音声合成・字幕・立ち絵）", "YouTube", "セリフ"),
    ("launch_voice", "音声合成ソフト（VOICEVOX など）を起動", "YouTube", "VV"),
    ("telop_editor", "テロップエディター（DavinTelop）", "YouTube", "Telop"),
    ("script_gen", "台本から一括生成（ゆっくり・解説）", "YouTube", "台本"),
    ("character_new", "キャラクター（立ち絵）を登録", "YouTube", "立絵"),
    ("regen_line", "選択したセリフの音声を作り直し", "YouTube", "再合"),
    ("transcribe_subs", "文字起こしで字幕を作成", "YouTube", "文字"),
    ("cps_check", "テロップの読みやすさチェック", "YouTube", "CPS"),
    ("thumbnail", "サムネイルを作成", "YouTube", "サム"),
    ("make_shorts", "ショート動画（縦型）を作成", "YouTube", "縦"),
    ("description", "概要欄テキスト（チャプター・クレジット）", "YouTube", "概要"),
    ("loudness", "ラウドネス測定（-14 LUFS に合わせる）", "YouTube", "LUFS"),
    ("slideshow", "選択画像でスライドショー", "YouTube", "スラ"),
    ("bgm_loop", "BGM を最後までループ", "YouTube", "BGM"),
    ("toggle_duck", "選択トラックのダッキング（BGM 自動下げ）切替", "YouTube", "Duck"),
    ("add_band", "テロップ帯（図形）を追加", "YouTube", "帯"),
    ("add_progress", "進行バーを追加", "YouTube", "進行"),
    ("se_pad", "SE パッドを表示", "YouTube", "SE"),
    ("preset_panel", "プリセット（エフェクト・テロップ・スケール）を表示", "YouTube", "Pre"),
    # ── AI・連携 ──
    ("command_palette", "コマンドパレット", "AI・連携", "⌘"),
    ("drag_zoom", "Drag & Zoom（モニターで囲んでズーム）", "AI・連携", "DZ"),
    ("ai_assist", "AI アシスト（Claude Code）を表示", "AI・連携", "AI"),
    ("ai_image", "AI 画像生成（ComfyUI）を表示", "AI・連携", "画像"),
    ("asset_panel", "アセット / お気に入りを表示", "AI・連携", "★"),
    ("favorite_media", "選択素材をお気に入りに追加", "AI・連携", "★+"),
    ("edit_in_photoshop", "画像を Photoshop で編集（保存で自動反映）", "AI・連携", "Ps"),
    ("frame_to_photoshop", "現在のフレームを Photoshop で描き込み", "AI・連携", "Ps□"),
    ("send_ae", "クリップを After Effects に送る", "AI・連携", "Ae"),
    ("send_blender", "クリップを Blender で開く", "AI・連携", "Bl"),
    ("render_blender", "クリップを Blender でレンダリング（自動でつなぐ）", "AI・連携", "Bl▶"),
    ("watch_folders", "監視フォルダ（自動取り込み）の設定", "AI・連携", "監視"),
    ("plugin_folder", "エフェクトプラグインのフォルダを開く", "AI・連携", "Plug"),
    # ── 3D / MMD / ノード ──
    ("node_editor", "ノードエディター（エフェクトをノードで管理）", "3D・MMD", "Node"),
    ("add_adjust", "調整レイヤーを追加", "3D・MMD", "調整"),
    ("beat_markers", "BGM のビートを検出してマーカー", "3D・MMD", "Beat"),
    ("text3d", "3D テキスト（Blender）", "3D・MMD", "3D字"),
    ("logo3d", "3D ロゴ板（Blender）", "3D・MMD", "3D板"),
    ("mmd_launch", "MMD（MikuMikuDance）を起動", "3D・MMD", "MMD"),
    ("mmd_lipsync", "セリフから MMD の口パク VMD を作る", "3D・MMD", "口VMD"),
    ("mmd_render", "MMD のモデルとモーションを Blender でレンダリング", "3D・MMD", "MMD▶"),
    ("mmd_tools_install", "Blender に MMD Tools を入れる", "3D・MMD", "MMDT"),
    ("reload_plugins", "エフェクトプラグインを再読み込み", "AI・連携", "Plug↻"),
    # ── テロップ ──
    ("telop_pos", "テロップの位置（9 か所から選ぶ）", "テロップ", "位置"),
    ("telop_style_all", "選んだテロップの見た目を一括適用", "テロップ", "一括"),
    ("telop_dict", "表記統一辞書（一括置換）", "テロップ", "辞書"),
    ("telop_from_text", "テキストからテロップを一括配置", "テロップ", "流込"),
    ("telop_auto_len", "テロップの長さを文字数で調整", "テロップ", "長さ"),
    ("telop_shift", "テロップをまとめてずらす", "テロップ", "ずら"),
    ("telop_fit_audio", "テロップを音声（セリフ）に合わせる", "テロップ", "音合"),
    ("telop_speaker_colors", "話者ごとにテロップの色を分ける", "テロップ", "話色"),
    ("markers_to_telops", "マーカーからテロップを作る", "テロップ", "M→T"),
    ("telops_to_markers", "テロップの位置にマーカー", "テロップ", "T→M"),
    # ── 整理・チェック ──
    ("marker_panel", "マーカー一覧を表示", "整理・チェック", "◆一覧"),
    ("marker_csv", "マーカーを CSV で書き出し", "整理・チェック", "◆CSV"),
    ("marker_chapters", "マーカーを YouTube チャプターでコピー", "整理・チェック", "◆章"),
    ("split_at_markers", "マーカーの位置ですべて分割", "整理・チェック", "◆分割"),
    ("next_gap", "次の隙間（黒）へ", "整理・チェック", "隙→"),
    ("prev_gap", "前の隙間（黒）へ", "整理・チェック", "←隙"),
    ("fill_gaps", "隙間を黒で埋める", "整理・チェック", "黒埋"),
    ("preflight", "書き出し前チェック", "整理・チェック", "✓"),
    ("fit_frame", "画面にフィット", "整理・チェック", "Fit"),
    ("fill_frame", "画面を埋める（フィル）", "整理・チェック", "Fill"),
    ("pip", "ピクチャー・イン・ピクチャー", "整理・チェック", "PiP"),
    ("ken_burns", "ケン・バーンズ（ゆっくりズーム）", "整理・チェック", "KB"),
    ("snapshot", "シーケンスのスナップショットを保存", "整理・チェック", "版"),
    ("unused_media", "未使用の素材を整理", "整理・チェック", "未使"),
    ("media_list", "使用素材リスト（CSV）を書き出し", "整理・チェック", "素材表"),
    ("edit_list", "編集リスト（テキスト）を書き出し", "整理・チェック", "編集表"),
    ("space_clips", "選んだクリップの間隔をそろえる", "整理・チェック", "間隔"),
    ("move_to_playhead", "選んだクリップを再生ヘッドへ移動", "整理・チェック", "→▼"),
    ("select_same_media", "同じ素材のクリップをすべて選択", "整理・チェック", "同素"),
    ("select_video_only", "選択を映像だけにする", "整理・チェック", "V選"),
    ("select_audio_only", "選択を音声だけにする", "整理・チェック", "A選"),
    ("replace_media", "素材の差し替え（使っている所すべて）", "整理・チェック", "差替"),
    ("seq_stats", "シーケンス情報（長さ・カット数・文字数）", "整理・チェック", "情報"),
    ("in_out_to_seq", "IN〜OUT を新しいシーケンスに", "整理・チェック", "IO→S"),
    # ── モーション（AE / Blender 風）──
    ("motion_panel", "モーションパネル（親子付け・3D・マスク・式）", "モーション", "Mo"),
    ("graph_editor", "グラフエディター（キーフレームの曲線）", "モーション", "Graph"),
    ("add_shape_layer", "図形（シェイプレイヤー）を追加", "モーション", "図形"),
    ("add_null", "ヌルを追加（親専用・見えない）", "モーション", "Null"),
    ("add_camera", "カメラを追加（3D レイヤー用）", "モーション", "Cam"),
    ("make_3d", "選んだクリップを 3D レイヤーに / 戻す", "モーション", "3D"),
    ("camera_move", "カメラワークのプリセット", "モーション", "CamM"),
    ("track_motion", "モーショントラッキング（モニターで囲む）", "モーション", "Trk"),
    ("parent_to_null", "選んだクリップをヌルにまとめる（親子付け）", "モーション", "親"),
    ("unparent", "親子付けを外す", "モーション", "親✕"),
    # ── 連携・管理 ──
    ("dynlink_import", "Dynamic Link: AE / Blender のファイルをクリップとして置く", "連携・管理", "DL"),
    ("dynlink_rerender", "Dynamic Link: 選んだクリップをレンダリングし直す", "連携・管理", "DL↻"),
    ("dynlink_open", "Dynamic Link: 元のアプリで開く", "連携・管理", "DL↗"),
    ("update_panel", "更新履歴（自動同期・前の版に戻す）", "連携・管理", "履歴"),
    ("production_panel", "制作管理（ショット・担当・期限）", "連携・管理", "管理"),
    ("shots_from_markers", "マーカーからショットを作る", "連携・管理", "◆→S"),
    # ── クリップ（ほかの編集ソフトにある機能）──
    ("select_at_playhead", "再生ヘッドのクリップを選択", "クリップ", "▼選"),
    ("duplicate_after", "選んだクリップをすぐ後ろに複製", "クリップ", "複製"),
    ("speed_ramp", "スピードランプ（速度カーブ）", "クリップ", "Ramp"),
    ("speed_ramp_clear", "スピードランプを外す", "クリップ", "Ramp✕"),
    ("clip_anim_in", "クリップの登場アニメ", "クリップ", "入"),
    ("clip_anim_out", "クリップの退場アニメ", "クリップ", "出"),
    ("nest", "ネスト（選んだクリップを 1 本にまとめる）", "クリップ", "Nest"),
    ("nest_open", "ネストを開く", "クリップ", "Nest↗"),
    ("take_add", "テイクを追加（別テイクを見比べる）", "クリップ", "Take+"),
    ("take_next", "次のテイクに切り替え", "クリップ", "Take▶"),
    ("match_loudness", "選んだ音声のラウドネスをそろえる", "クリップ", "LUFS="),
    ("compare_seq", "シーケンスを比較", "クリップ", "比較"),
    ("collect_files", "ファイルの収集（素材をプロジェクトに集める）", "クリップ", "収集"),
    # ── 変換・仕上げ ──
    ("transcode", "トランスコード（DNxHR / DNxHD / ProRes / QuickTime …）", "変換・仕上げ", "⇄"),
    ("export_audio_only", "音声だけ書き出し（WAV / AAC / FLAC）", "変換・仕上げ", "♪書"),
    ("export_gif", "GIF で書き出し", "変換・仕上げ", "GIF"),
    ("export_images", "連番画像で書き出し（PNG / JPEG）", "変換・仕上げ", "連番"),
    ("archive_project", "プロジェクトを素材ごと zip に", "変換・仕上げ", "zip"),
    ("thumb_candidates", "サムネイル候補を自動で選ぶ", "変換・仕上げ", "サ候"),
    ("color_match", "カラーマッチ（お手本の色に合わせる）", "変換・仕上げ", "色合"),
    ("auto_color", "オートカラー（自動レベル補正）", "変換・仕上げ", "自色"),
    ("stabilize", "手ぶれ補正", "変換・仕上げ", "防振"),
    ("flip_h", "左右反転", "変換・仕上げ", "⇋"),
    ("flip_v", "上下反転", "変換・仕上げ", "⇵"),
    ("split_screen", "分割画面（2 / 4 / 9 分割）", "変換・仕上げ", "分割"),
    ("j_cut", "J カット（音を先に）", "変換・仕上げ", "J"),
    ("l_cut", "L カット（音を後まで）", "変換・仕上げ", "L"),
    ("sync_audio", "音声で同期（2 つのクリップを波形で合わせる）", "変換・仕上げ", "同期"),
    ("still_length", "静止画の長さをそろえる", "変換・仕上げ", "静長"),
    ("split_every", "N 秒ごとに分割", "変換・仕上げ", "N秒"),
    ("reverse_order", "選んだクリップを逆順に", "変換・仕上げ", "逆順"),
    ("shuffle_order", "選んだクリップをシャッフル", "変換・仕上げ", "混"),
    ("fit_to_markers", "マーカー（ビート）に合わせて並べる", "変換・仕上げ", "◆並"),
    ("media_usage", "素材の使用箇所を表示", "変換・仕上げ", "使用"),
    ("burn_timecode", "タイムコードを焼き込み", "変換・仕上げ", "TC"),
    ("watermark", "ウォーターマーク（ロゴを全編に）", "変換・仕上げ", "透かし"),
    ("letterbox", "シネスコの黒帯", "変換・仕上げ", "帯"),
    ("bars_tone", "カラーバーと 1kHz トーン", "変換・仕上げ", "Bars"),
    ("split_long_telops", "長いテロップを分割", "変換・仕上げ", "T分"),
    ("telops_by_length", "テロップを文字数で IN〜OUT に並べる", "変換・仕上げ", "T並"),
]

# 後から追加したコマンドの既定キー（プリセットの他のキーと重ならなければ割り当てる）
NEW_COMMAND_KEYS = {"command_palette": ["Ctrl+Shift+P"], "drag_zoom": ["Ctrl+Alt+Z"],
                    "script_gen": ["Ctrl+Alt+Y"], "ai_assist": ["Ctrl+Alt+C"],
                    "telop_editor": ["Ctrl+Shift+T"], "voice_panel": ["Ctrl+Shift+Y"],
                    "node_editor": ["Ctrl+Shift+N"], "preflight": ["Ctrl+Alt+K"],
                    "next_gap": ["Ctrl+Alt+Right"], "prev_gap": ["Ctrl+Alt+Left"],
                    "marker_panel": ["Ctrl+Alt+M"], "snapshot": ["Ctrl+Alt+S"],
                    "graph_editor": ["Shift+F3"], "motion_panel": ["Ctrl+Alt+G"],
                    "select_at_playhead": ["D"], "take_next": ["Ctrl+Alt+T"], "nest": ["Ctrl+Alt+N"]}

COMMAND_INFO = OrderedDict((c[0], c) for c in COMMANDS)
CATEGORIES = list(OrderedDict.fromkeys(c[2] for c in COMMANDS))

# ─────────────────────────────────────────────
#  プリセット
# ─────────────────────────────────────────────

# Avid Media Composer 標準配列に準拠したキー。
# AVID_EXTRA は Avid 標準では未割り当ての機能に TeloCut が割り当てたもの。
AVID_STANDARD = {
    "play_stop": ["Space"], "shuttle_j": ["J"], "shuttle_k": ["K"], "shuttle_l": ["L"],
    "play_in_out": ["P"],
    "step_back_1": ["3", "Left"], "step_fwd_1": ["4", "Right"],
    "step_back_10": ["1"], "step_fwd_10": ["2"],
    "prev_edit": ["A"], "next_edit": ["S"],
    "goto_start": ["Home"], "goto_end": ["End"],
    "goto_in": ["Q"], "goto_out": ["W"],
    "mark_in": ["E", "I"], "mark_out": ["R", "O"], "mark_clip": ["T"],
    "clear_in": ["D"], "clear_out": ["F"], "clear_both": ["G"],
    "lift": ["Z"], "extract": ["X"], "copy_clipboard": ["C"],
    "splice_in": ["V"], "overwrite": ["B"], "toggle_monitor": ["N"],
    "trim_mode": ["U"], "trim_m10": ["M"], "trim_m1": [","], "trim_p1": ["."], "trim_p10": ["/"],
    "trim_side_ab": ["["], "trim_side_b": ["]"],
    "focus": ["H"], "delete": ["Delete", "Backspace"],
    "undo": ["Ctrl+Z"], "redo": ["Ctrl+R"],
    "save_project": ["Ctrl+S"], "new_bin": ["Ctrl+N"], "new_sequence": ["Ctrl+Shift+N"],
    "select_all_tracks": ["Ctrl+A"], "deselect_all_tracks": ["Ctrl+Shift+A"],
    "zoom_in": ["Ctrl+]"], "zoom_out": ["Ctrl+["], "zoom_fit": ["Ctrl+/"],
    "fullscreen": ["Ctrl+Shift+F"],
    "goto_tc": ["Ctrl+G"],
}
AVID_EXTRA = {
    # トリムモード中の P は「A 側」（モード外では Play IN to OUT）
    "add_edit": ["Ctrl+K"], "add_edit_all": ["Ctrl+Shift+K"],
    "ripple_delete": ["Shift+Delete"], "match_frame": ["Shift+F"],
    "add_marker": ["Shift+M"], "add_telop": ["Shift+T"],
    "toggle_enabled": ["Shift+E"], "toggle_link": ["Ctrl+L"], "add_fades": ["Ctrl+D"],
    "copy_clipboard": ["C", "Ctrl+C"], "paste": ["Ctrl+V"], "cut": ["Ctrl+X"],
    "tool_select": ["F9"], "tool_yellow": ["F10"], "tool_roll": ["F11"], "tool_ripple": ["F12"],
    "toggle_v1": ["Ctrl+1"], "toggle_v2": ["Ctrl+2"], "toggle_v3": ["Ctrl+3"], "toggle_v4": ["Ctrl+4"],
    "toggle_a1": ["Ctrl+5"], "toggle_a2": ["Ctrl+6"], "toggle_a3": ["Ctrl+7"], "toggle_a4": ["Ctrl+8"],
    "render_in_out": ["Ctrl+Alt+R"], "render_all": ["Ctrl+Alt+Shift+R"],
    "import": ["Ctrl+I"], "export": ["Ctrl+E"], "save_as": ["Ctrl+Shift+S"],
    "open_project": ["Ctrl+O"], "close_project": ["Ctrl+W"],
    "keyboard_settings": ["Ctrl+Alt+K"], "shortcuts_help": ["F1"], "snap_toggle": ["Ctrl+Alt+S"],
    "select_all_clips": ["Ctrl+Alt+A"], "deselect_all": ["Escape"],
    "loop_toggle": ["Ctrl+Alt+L"],
    "add_dissolve": ["Ctrl+T"], "speed": ["Ctrl+Alt+J"], "freeze_frame": ["Ctrl+Alt+H"],
    "paste_attributes": ["Ctrl+Alt+V"], "replace_edit": ["Ctrl+Alt+E"], "close_gaps": ["Ctrl+Alt+G"],
    "select_forward": ["Ctrl+Alt+Right"], "subclip": ["Ctrl+Alt+U"], "find_bin": ["Ctrl+Alt+B"],
    "project_find": ["Ctrl+F"], "export_frame": ["Ctrl+Shift+E"], "import_interchange": ["Ctrl+Alt+I"],
    "tc_window": ["Ctrl+Alt+T"],
}

PREMIERE = {
    "play_stop": ["Space"], "shuttle_j": ["J"], "shuttle_k": ["K"], "shuttle_l": ["L"],
    "play_in_out": ["Ctrl+Shift+Space"],
    "step_back_1": ["Left"], "step_fwd_1": ["Right"],
    "step_back_5": ["Shift+Left"], "step_fwd_5": ["Shift+Right"],
    "prev_edit": ["Up"], "next_edit": ["Down"], "goto_start": ["Home"], "goto_end": ["End"],
    "goto_in": ["Shift+I"], "goto_out": ["Shift+O"],
    "mark_in": ["I"], "mark_out": ["O"], "mark_clip": ["X"],
    "clear_in": ["Ctrl+Shift+I"], "clear_out": ["Ctrl+Shift+O"], "clear_both": ["Ctrl+Shift+X"],
    "add_marker": ["M"], "splice_in": [","], "overwrite": ["."], "lift": [";"], "extract": ["'"],
    "copy_clipboard": ["Ctrl+C"], "paste": ["Ctrl+V"], "cut": ["Ctrl+X"],
    "add_edit": ["Ctrl+K"], "add_edit_all": ["Ctrl+Shift+K"],
    "delete": ["Delete", "Backspace"], "ripple_delete": ["Shift+Delete"],
    "ripple_trim_prev": ["Q"], "ripple_trim_next": ["W"], "match_frame": ["F"],
    "toggle_enabled": ["Shift+E"], "toggle_link": ["Ctrl+L"],
    "add_telop": ["T"], "undo": ["Ctrl+Z"], "redo": ["Ctrl+Shift+Z", "Ctrl+Y"],
    "select_all_clips": ["Ctrl+A"], "deselect_all": ["Ctrl+Shift+A"],
    "trim_mode": ["Shift+T"], "trim_m1": ["Ctrl+Left"], "trim_p1": ["Ctrl+Right"],
    "trim_m10": ["Ctrl+Shift+Left"], "trim_p10": ["Ctrl+Shift+Right"],
    "tool_select": ["V"], "tool_ripple": ["B"], "tool_roll": ["N"], "tool_slip": ["Y"],
    "tool_slide": ["U"], "tool_razor": ["C"], "tool_hand": ["H"], "tool_zoom": ["Z"],
    "snap_toggle": ["S"], "zoom_in": ["="], "zoom_out": ["-"], "zoom_fit": ["\\"],
    "fullscreen": ["Ctrl+`"], "render_in_out": ["Return"],
    "save_project": ["Ctrl+S"], "open_project": ["Ctrl+O"], "new_project": ["Ctrl+Alt+N"],
    "new_bin": ["Ctrl+B"], "new_sequence": ["Ctrl+N"], "close_project": ["Ctrl+Shift+W"],
    "save_as": ["Ctrl+Shift+S"], "import": ["Ctrl+I"], "export": ["Ctrl+M"],
    "goto_tc": ["Ctrl+G"], "shortcuts_help": ["F1"], "keyboard_settings": ["Ctrl+Alt+K"],
    "add_dissolve": ["Ctrl+D"], "add_fades": ["Ctrl+Shift+D"], "speed": ["Ctrl+R"],
    "paste_attributes": ["Ctrl+Alt+V"], "freeze_frame": ["Shift+N"], "select_forward": ["A"],
    "project_find": ["Ctrl+F"], "export_frame": ["Ctrl+Shift+E"], "import_interchange": ["Ctrl+Alt+I"],
    "find_bin": ["Shift+F"], "close_gaps": ["Ctrl+Alt+G"], "replace_edit": ["Ctrl+Alt+E"],
    "subclip": ["Ctrl+U"], "tc_window": ["Ctrl+Alt+T"],
}

PRESETS = OrderedDict([
    ("avid", "Avid Media Composer 標準"),
    ("premiere", "Premiere Pro 風"),
])


def add_new_keys(km: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """キー設定に、まだ無い新コマンドの既定キーを足す（使用中のキーは足さない）。"""
    used = {k for v in km.values() for k in v}
    for cid, keys in NEW_COMMAND_KEYS.items():
        if cid not in km:
            km[cid] = [k for k in keys if k not in used]
            used |= set(km[cid])
    return km


def preset_keymap(name: str) -> Dict[str, List[str]]:
    if name == "premiere":
        return add_new_keys({k: list(v) for k, v in PREMIERE.items()})
    km = {k: list(v) for k, v in AVID_STANDARD.items()}
    for k, v in AVID_EXTRA.items():
        km[k] = list(dict.fromkeys(km.get(k, []) + v))
    return add_new_keys(km)


def is_avid_standard(cmd: str, key: str) -> bool:
    return key in AVID_STANDARD.get(cmd, [])


def conflicts(keymap: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """同じキーに複数コマンドが割り当てられているもの {key: [cmd, ...]}。
    トリムモード専用キーとの重複は文脈で切り替わるので除外。"""
    trim_ctx = {"trim_m10", "trim_m1", "trim_p1", "trim_p10", "trim_side_a", "trim_side_ab", "trim_side_b"}
    by_key: Dict[str, List[str]] = {}
    for cmd, keys in keymap.items():
        for k in keys:
            by_key.setdefault(k.lower(), []).append(cmd)
    out = {}
    for k, cmds in by_key.items():
        if len(cmds) > 1 and len([c for c in cmds if c not in trim_ctx]) > 1:
            out[k] = cmds
    return out
