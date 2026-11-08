# -*- coding: utf-8 -*-
"""
テキストエディタ編集型リネームツール (LiName 代替)
=================================================

LiName と同じ「ファイル一覧をテキストエディタで編集して保存 → 反映」方式を、
Unicode (UTF-8) で正しく扱えるようにした Python スクリプト。

【経緯】LiName はサロゲートペア文字 (絵文字・CJK拡張B以降の漢字など U+10000 以上) を
リストファイルに CESU-8 (UTF-8 として不正なバイト列) で書き出すバグがあり、
UTF-8 対応エディタで開くと置換文字 U+FFFD に化け、保存時にファイル名が壊れる。
本スクリプトは Python の Unicode 処理で正しい UTF-8 を出力する。

【使い方】
  フォルダやファイルを同じフォルダの text_renamer.bat に
  ドラッグ＆ドロップして実行できます。
  python text_renamer.py <ファイルまたはフォルダ> ...
    (複数指定可。フォルダ指定時は中のファイルを再帰的に列挙)
  python text_renamer.py
    (対象フォルダの入力を求めます)
  python text_renamer.py --rollback
    (直前のリネームだけを1回ロールバックします)

  エディタは同じフォルダの text_renamer.ini で設定できます。
  初期値は `code --wait` (Visual Studio Code) です。
  削除・リネーム前の確認は `operation.confirm` で設定できます。
  初期値は `true` です。
  サブフォルダ検索は `search.include_subfolders` で設定できます。
  初期値は `true` です。
  リネーム履歴は同じフォルダの text_renamer_history.txt に保存されます。

  1. 一覧ファイル (UTF-8 with BOM) を既定のエディタで開く
  2. 「番号<TAB>サイズ(KB)<TAB>フルパス」形式。
     サイズは参考表示で、フルパスを書き換えて保存し、エディタを閉じる
     - パス変更 → リネーム (別フォルダなら移動。無いフォルダは自動作成)
     - 行頭に ! → ゴミ箱へ削除
     - 行頭に !! → ゴミ箱を経由せず直接削除
     - 行を消す → 何もしない
  3. ファイルリストを保存すると自動的に反映される
     (保存せずにエディタを閉じるとキャンセル)

【注意】LiName 同様、ファイル名に TAB や改行が含まれるファイルは扱えない。
"""

import os
import configparser
import shlex
import subprocess
import sys
import tempfile
import time

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "text_renamer.ini"
)
HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "text_renamer_history.txt"
)


def get_editor_command():
    """設定ファイルからエディタコマンドを読み込む"""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    command = config.get("editor", "command", fallback="code --wait").strip()
    if not command:
        raise ValueError(f"設定ファイルのエディタコマンドが空です: {CONFIG_PATH}")
    return [
        argument[1:-1]
        if len(argument) >= 2 and argument[0] == argument[-1] == '"'
        else argument
        for argument in shlex.split(command, posix=False)
    ]


def should_confirm_operations():
    """削除・リネーム前に確認する設定を読み込む"""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    return config.getboolean("operation", "confirm", fallback=True)


def should_include_subfolders():
    """フォルダ指定時にサブフォルダを検索する設定を読み込む"""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    return config.getboolean("search", "include_subfolders", fallback=True)


def open_editor(path):
    """設定されたエディタで一覧ファイルを開く"""
    command = get_editor_command()
    try:
        return subprocess.Popen(command + [path])
    except FileNotFoundError:
        print(f"エディタが見つかりません: {command[0]}")
        print(f"設定ファイルを確認してください: {CONFIG_PATH}")
        sys.exit(1)
    except OSError as e:
        print(f"エディタの起動に失敗しました: {e}")
        sys.exit(1)


def normalize_argument(argument):
    """引数全体を囲む引用符を取り除く"""
    argument = argument.strip()
    while len(argument) >= 2 and argument[0] == argument[-1] == '"':
        argument = argument[1:-1].strip()
    return argument


def wait_for_file_save(path, editor_process):
    """一覧ファイルの保存を検知するまで待つ"""
    initial_stat = os.stat(path)
    initial_signature = (initial_stat.st_mtime_ns, initial_stat.st_size)
    print("ファイルリストの保存を待機しています...")

    while True:
        if editor_process.poll() is not None:
            print("エディタが閉じられたため、処理をキャンセルしました")
            return False
        current_stat = os.stat(path)
        current_signature = (current_stat.st_mtime_ns, current_stat.st_size)
        if current_signature != initial_signature:
            print("ファイルリストの保存を検知しました。")
            return True
        time.sleep(0.5)


def collect_files(targets):
    """引数 (ファイル/フォルダ) から対象パスの一覧を収集する"""
    files = []
    include_subfolders = should_include_subfolders()
    for t in targets:
        if os.path.isdir(t):
            for root, dirs, names in os.walk(t):
                if not include_subfolders:
                    dirs.clear()
                for n in names:
                    files.append(os.path.join(root, n))
        elif os.path.exists(t):
            files.append(t)
        else:
            print(f"警告: {t} は存在しないため無視しました")
    return sorted(files)


def send_to_recycle_bin(path):
    """Windows の SHFileOperationW でゴミ箱へ移動 (依存ライブラリ不要)"""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", ctypes.c_ubyte),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]

    buf = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0\0")
    shf = SHFILEOPSTRUCTW()
    shf.hwnd = None
    shf.wFunc = 3  # FO_DELETE
    shf.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    shf.pTo = None
    shf.fFlags = 0x50 | 0x40  # FOF_ALLOWUNDO (ゴミ箱) | FOF_NOCONFIRMATION
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(shf))


def save_rename_history(pairs):
    """直前の処理で成功したリネームだけを履歴へ保存する"""
    with open(HISTORY_PATH, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("# 修正前\t修正後\n")
        for old, new in pairs:
            f.write(f"{old}\t{new}\n")


def rollback_last_renames():
    """履歴にあるリネームを元に戻し、履歴を1回で消費する"""
    if not os.path.isfile(HISTORY_PATH):
        print("ロールバックできる履歴がありません")
        return

    pairs = []
    with open(HISTORY_PATH, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            if "\t" not in line:
                raise ValueError(f"履歴ファイルの形式が不正です: {line[:50]}")
            old, new = line.split("\t", 1)
            pairs.append((old, new))

    if not pairs:
        print("ロールバックできるリネーム履歴がありません")
        os.remove(HISTORY_PATH)
        return

    ok = ng = 0
    try:
        for old, new in reversed(pairs):
            try:
                os.rename(new, old)
                ok += 1
            except OSError as e:
                print(f"ロールバック失敗: {new} -> {old} : {e}")
                ng += 1
    finally:
        os.remove(HISTORY_PATH)

    print(f"ロールバック完了: 成功 {ok} 件 / 失敗 {ng} 件")


def main():
    targets = [normalize_argument(argument) for argument in sys.argv[1:]]
    if targets == ["--rollback"]:
        rollback_last_renames()
        return
    if "--rollback" in targets:
        print("エラー: --rollback は単独で指定してください")
        sys.exit(1)

    if not targets:
        target_dir = normalize_argument(
            input("対象フォルダを入力してください: ")
        )
        if not target_dir:
            print("対象フォルダが入力されませんでした")
            sys.exit(1)
        targets = [target_dir]

    files = collect_files(targets)
    if not files:
        print("対象ファイルがありません")
        sys.exit(1)

    # 一覧は BOM 付き UTF-8 で出力 (エディタの文字コード自動判別を確実にするため)
    fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="renamelist_")
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(
            "# サイズ(KB)は参考表示。フルパスを書き換えて保存。\n"
        )
        f.write("# 行頭 ! でゴミ箱削除、!! で直接削除、行削除で何もしない\n")
        for i, p in enumerate(files):
            size_kb = os.path.getsize(p) // 1024
            f.write(f"{i:05d}\t{size_kb:>10,}\t{p}\n")

    print(f"{len(files)} 件を一覧に出力しました。エディタで編集・保存して閉じてください。")
    editor_process = open_editor(list_path)
    if not wait_for_file_save(list_path, editor_process):
        os.remove(list_path)
        return

    # 読み戻しは BOM 付き UTF-8。不正バイトがあれば例外で止める (黙って壊さないため)
    with open(list_path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()

    orig_map = {f"{i:05d}": p for i, p in enumerate(files)}
    pairs, deletes = [], []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        delete_requested = None
        if line.startswith("!!"):
            delete_requested = "direct"
            line = line[2:].lstrip()
        elif line.startswith("!"):
            delete_requested = "recycle"
            line = line[1:].lstrip()
        columns = line.split("\t", 2)
        if len(columns) != 3:
            print(f"警告: 列数が不正な行を無視: {line[:50]}")
            continue
        num, _size_kb, path = columns
        old = orig_map.get(num.strip())
        if old is None:
            print(f"警告: 不明な番号の行を無視: {line[:50]}")
            continue
        path = path.strip()
        if delete_requested:
            deletes.append((old, delete_requested))
        elif path.startswith("!!"):
            deletes.append((old, "direct"))
        elif path.startswith("!"):
            deletes.append((old, "recycle"))
        elif os.path.abspath(path) != os.path.abspath(old):
            pairs.append((old, path))

    if not pairs and not deletes:
        print("変更がないため、処理を終了します")
        os.remove(list_path)
        return

    if should_confirm_operations():
        print("\n編集内容を確認してください:")
        for old, new in pairs:
            print(f"  リネーム: {old} -> {new}")
        for path, delete_mode in deletes:
            label = "直接削除" if delete_mode == "direct" else "ゴミ箱へ削除"
            print(f"  {label}: {path}")
        answer = input("この内容で作業を開始しますか？ [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("キャンセルしました")
            os.remove(list_path)
            return


    ok = ng = 0
    rename_history = []
    for old, new in pairs:
        try:
            new_dir = os.path.dirname(new)
            if new_dir and not os.path.isdir(new_dir):
                os.makedirs(new_dir)  # LiName 同様、移動先フォルダは自動作成
            os.rename(old, new)
            rename_history.append((os.path.abspath(old), os.path.abspath(new)))
            ok += 1
        except OSError as e:
            print(f"失敗: {old} -> {new} : {e}")
            ng += 1

    for p, delete_mode in deletes:
        try:
            if delete_mode == "direct":
                os.remove(p)
            elif sys.platform == "win32":
                send_to_recycle_bin(p)
            else:
                os.remove(p)  # テスト環境では通常削除
            ok += 1
        except OSError as e:
            print(f"削除失敗: {p} : {e}")
            ng += 1

    save_rename_history(rename_history)
    if rename_history:
        print(f"リネーム履歴を保存しました: {HISTORY_PATH}")
    print(f"完了: 成功 {ok} 件 / 失敗 {ng} 件")
    os.remove(list_path)


if __name__ == "__main__":
    main()
