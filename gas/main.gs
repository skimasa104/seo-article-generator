/**
 * SEO記事自動生成 - Google Apps Script
 *
 * スプレッドシートからキーワードを読み取り、
 * Mac mini上のserver.pyにリクエストを送信してパイプラインを実行する。
 *
 * ■ スプレッドシートの列構成（1行目がヘッダー）:
 *   A列: キーワード（例: AGA 横浜）
 *   B列: ジャンル（例: aga, ed, hair_removal）
 *   C列: サイト設定ファイル（例: sites/aurora_clinic.json）
 *   D列: カテゴリ（例: AGA）
 *   E列: ステータス（自動更新: 未実行 / 実行中 / 完了 / エラー）
 *   F列: ジョブID（自動記入）
 *   G列: 実行日時（自動記入）
 *   H列: 備考（自動記入: エラー内容等）
 *
 * ■ 使い方:
 *   1. スプレッドシートにこのスクリプトを紐づける
 *   2. SERVER_URL を Mac mini の ngrok/Cloudflare Tunnel URL に変更
 *   3. A〜D列にデータを入力し、E列を「未実行」にする
 *   4. メニュー「SEO記事生成」→「選択行を実行」または「未実行を一括実行」
 */

// ========================================
// 設定
// ========================================

/** server.py の公開URL（ngrok or Cloudflare Tunnel） */
const SERVER_URL = "https://your-server-url.ngrok-free.app";

/** リクエストタイムアウト（秒） */
const REQUEST_TIMEOUT = 30;

/** ステータス確認の間隔（秒） */
const POLL_INTERVAL = 60;

/** ステータス確認の最大回数（60秒 × 30回 = 30分） */
const MAX_POLL_COUNT = 30;


// ========================================
// メニュー
// ========================================
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("SEO記事生成")
    .addItem("選択行を実行", "runSelectedRow")
    .addItem("未実行を一括実行", "runAllPending")
    .addSeparator()
    .addItem("ステータス更新", "updateAllStatuses")
    .addItem("ヘルスチェック", "healthCheck")
    .addToUi();
}


// ========================================
// パイプライン実行
// ========================================

/**
 * 選択中の行のパイプラインを実行
 */
function runSelectedRow() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var row = sheet.getActiveRange().getRow();

  if (row <= 1) {
    SpreadsheetApp.getUi().alert("2行目以降を選択してください。");
    return;
  }

  runRow(sheet, row);
}


/**
 * ステータスが「未実行」の行を上から順に実行
 */
function runAllPending() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var lastRow = sheet.getLastRow();
  var count = 0;

  for (var row = 2; row <= lastRow; row++) {
    var status = sheet.getRange(row, 5).getValue();
    if (status === "未実行" || status === "") {
      runRow(sheet, row);
      count++;
      // API負荷を分散するため少し待つ
      Utilities.sleep(3000);
    }
  }

  if (count === 0) {
    SpreadsheetApp.getUi().alert("未実行のキーワードがありません。");
  } else {
    SpreadsheetApp.getUi().alert(count + "件のパイプラインを開始しました。\nステータスは自動更新されます。");
  }
}


/**
 * 指定行のパイプラインを実行
 */
function runRow(sheet, row) {
  var keyword = sheet.getRange(row, 1).getValue().toString().trim();
  var genre = sheet.getRange(row, 2).getValue().toString().trim();
  var site = sheet.getRange(row, 3).getValue().toString().trim();
  var category = sheet.getRange(row, 4).getValue().toString().trim();

  if (!keyword) {
    sheet.getRange(row, 5).setValue("エラー");
    sheet.getRange(row, 8).setValue("キーワードが空です");
    return;
  }

  if (!genre) {
    sheet.getRange(row, 5).setValue("エラー");
    sheet.getRange(row, 8).setValue("ジャンルが空です");
    return;
  }

  // デフォルトサイト設定
  if (!site) {
    site = "sites/aurora_clinic.json";
  }

  // リクエスト送信
  var payload = {
    keyword: keyword,
    genre: genre,
    site: site,
    category: category,
  };

  try {
    var response = postRequest("/run", payload);

    if (response.error) {
      sheet.getRange(row, 5).setValue("エラー");
      sheet.getRange(row, 8).setValue(response.error);
      return;
    }

    // 成功: ジョブ情報を記入
    sheet.getRange(row, 5).setValue("実行中");
    sheet.getRange(row, 6).setValue(response.job_id);
    sheet.getRange(row, 7).setValue(new Date());
    sheet.getRange(row, 8).setValue("");

  } catch (e) {
    sheet.getRange(row, 5).setValue("エラー");
    sheet.getRange(row, 8).setValue("接続エラー: " + e.message);
  }
}


// ========================================
// ステータス確認
// ========================================

/**
 * 「実行中」の全行のステータスを更新
 */
function updateAllStatuses() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var lastRow = sheet.getLastRow();
  var updated = 0;

  for (var row = 2; row <= lastRow; row++) {
    var status = sheet.getRange(row, 5).getValue();
    if (status === "実行中") {
      var jobId = sheet.getRange(row, 6).getValue().toString().trim();
      if (jobId) {
        updateRowStatus(sheet, row, jobId);
        updated++;
      }
    }
  }

  if (updated === 0) {
    SpreadsheetApp.getUi().alert("実行中のジョブはありません。");
  }
}


/**
 * 指定行のステータスを更新
 */
function updateRowStatus(sheet, row, jobId) {
  try {
    var response = getRequest("/status?job_id=" + jobId);

    if (response.error) {
      sheet.getRange(row, 8).setValue("確認エラー: " + response.error);
      return;
    }

    var jobStatus = response.status;

    if (jobStatus === "running") {
      // まだ実行中
      return;
    }

    if (jobStatus === "success") {
      sheet.getRange(row, 5).setValue("完了");
      sheet.getRange(row, 8).setValue("正常完了");
    } else {
      sheet.getRange(row, 5).setValue("エラー");
      sheet.getRange(row, 8).setValue("結果: " + jobStatus);
    }

  } catch (e) {
    sheet.getRange(row, 8).setValue("確認エラー: " + e.message);
  }
}


/**
 * トリガーで定期的にステータスを確認（1分ごと）
 * 手動で設定: トリガー → updateAllStatuses → 時間ベース → 1分
 */


// ========================================
// ヘルスチェック
// ========================================

/**
 * サーバーの稼働状態を確認
 */
function healthCheck() {
  try {
    var response = getRequest("/health");
    var msg = "サーバー状態: " + response.status +
              "\n時刻: " + response.time +
              "\n実行中ジョブ: " + response.active_jobs;
    SpreadsheetApp.getUi().alert(msg);
  } catch (e) {
    SpreadsheetApp.getUi().alert("サーバーに接続できません。\n\n" + e.message +
      "\n\nSERVER_URL を確認してください: " + SERVER_URL);
  }
}


// ========================================
// HTTP通信
// ========================================

/**
 * POSTリクエスト
 */
function postRequest(path, payload) {
  var url = SERVER_URL + path;
  var options = {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
    headers: {
      "Accept": "application/json",
    },
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  var body = JSON.parse(response.getContentText());

  if (code >= 400) {
    throw new Error("HTTP " + code + ": " + (body.error || response.getContentText()));
  }

  return body;
}


/**
 * GETリクエスト
 */
function getRequest(path) {
  var url = SERVER_URL + path;
  var options = {
    method: "get",
    muteHttpExceptions: true,
    headers: {
      "Accept": "application/json",
    },
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  var body = JSON.parse(response.getContentText());

  if (code >= 400) {
    throw new Error("HTTP " + code + ": " + (body.error || response.getContentText()));
  }

  return body;
}
