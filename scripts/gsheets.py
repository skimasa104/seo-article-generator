#!/usr/bin/env python3
"""
Google Sheets への読み書き用ユーティリティ。

初回セットアップ:
  1. Google Cloud Console で OAuth クライアントID（デスクトップアプリ）を作成
  2. ダウンロードした JSON をプロジェクト直下に `client_secret.json` として保存
  3. `python3 scripts/gsheets.py auth` を実行 → ブラウザで認証 → token.json が生成される

使い方:
  # セルに書き込み（タブ/改行区切りの2次元データ）
  python3 scripts/gsheets.py write <SPREADSHEET_ID> '<シート名>!E3' --tsv-file /tmp/data.tsv
  python3 scripts/gsheets.py write <SPREADSHEET_ID> 'テスト!E3' --tsv - < /tmp/data.tsv

  # セルを読む
  python3 scripts/gsheets.py read <SPREADSHEET_ID> 'テスト!A1:G30'

  # シート一覧
  python3 scripts/gsheets.py sheets <SPREADSHEET_ID>
"""
import sys
import os
import argparse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_SECRET = os.path.join(ROOT, "client_secret.json")
TOKEN = os.path.join(ROOT, "token.json")


def get_credentials():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit(
                    f"Error: {CLIENT_SECRET} が見つかりません。\n"
                    "Google Cloud Console で OAuth クライアントID（デスクトップアプリ）を作成し、\n"
                    "JSON を client_secret.json としてプロジェクト直下に保存してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return creds


def get_service():
    return build("sheets", "v4", credentials=get_credentials())


def cmd_auth(args):
    get_credentials()
    print("認証完了。token.json を保存しました。")


def cmd_sheets(args):
    svc = get_service()
    meta = svc.spreadsheets().get(spreadsheetId=args.spreadsheet_id).execute()
    print(f"Spreadsheet: {meta.get('properties', {}).get('title', '')}")
    for s in meta.get("sheets", []):
        p = s["properties"]
        print(f"  - {p['title']}  ({p['gridProperties'].get('rowCount')}x{p['gridProperties'].get('columnCount')})")


def cmd_read(args):
    svc = get_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=args.spreadsheet_id, range=args.range
    ).execute()
    for row in res.get("values", []):
        print("\t".join(row))


def cmd_write(args):
    if args.tsv_file:
        if args.tsv_file == "-":
            raw = sys.stdin.read()
        else:
            with open(args.tsv_file, encoding="utf-8") as f:
                raw = f.read()
    elif args.tsv is not None:
        raw = args.tsv if args.tsv != "-" else sys.stdin.read()
    else:
        sys.exit("Error: --tsv または --tsv-file を指定してください。")

    rows = [line.split("\t") for line in raw.rstrip("\n").split("\n")]
    svc = get_service()
    res = svc.spreadsheets().values().update(
        spreadsheetId=args.spreadsheet_id,
        range=args.range,
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
    print(f"Updated {res.get('updatedCells')} cells in range {res.get('updatedRange')}")


def main():
    parser = argparse.ArgumentParser(description="Google Sheets 読み書きユーティリティ")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="OAuth 認証（初回のみ）")
    p_auth.set_defaults(func=cmd_auth)

    p_sheets = sub.add_parser("sheets", help="シート一覧を表示")
    p_sheets.add_argument("spreadsheet_id")
    p_sheets.set_defaults(func=cmd_sheets)

    p_read = sub.add_parser("read", help="セル範囲を読む")
    p_read.add_argument("spreadsheet_id")
    p_read.add_argument("range", help="例: 'テスト!A1:G30'")
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write", help="セル範囲に書き込む")
    p_write.add_argument("spreadsheet_id")
    p_write.add_argument("range", help="書き込み開始セル。例: 'テスト!E3'")
    p_write.add_argument("--tsv", help="タブ/改行区切りデータ。'-' で標準入力")
    p_write.add_argument("--tsv-file", help="タブ/改行区切りデータのファイルパス。'-' で標準入力")
    p_write.set_defaults(func=cmd_write)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
