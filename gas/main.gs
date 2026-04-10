/**
 * SEO記事自動生成 - Google Apps Script
 *
 * スプレッドシートからキーワードを読み取り、
 * Mac mini上のserver.pyにリクエストを送信してパイプラインを実行する。
 *
 * ■ スプレッドシートの列構成（1行目がヘッダー）:
 *   A列: キーワード（例: AGA 横浜）
 *   B列: ジャンル（例: aga, ed, hair_removal, phimosis, diet）
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

const HEADER_VALUES = [[
  "キーワード",
  "ジャンル",
  "投稿サイト",
  "カテゴリ",
  "ステータス",
  "ジョブID",
  "実行日時",
  "備考"
]];

const GENRE_OPTIONS = ["aga", "ed", "hair_removal", "phimosis", "diet"];
const SITE_OPTIONS = [
  "sites/aurora_clinic.json",
  "sites/ashitano_clinic.json",
  "sites/mame_clinic.json",
  "sites/utu_yobo.json"
];
const STATUS_OPTIONS = ["未実行", "実行中", "完了", "エラー"];
const SETUP_ROW_COUNT = 1000;
const GENRE_LABEL_MAP = {
  "AGA": "aga",
  "aga": "aga",
  "ED": "ed",
  "ed": "ed",
  "医療脱毛": "hair_removal",
  "脱毛": "hair_removal",
  "hair_removal": "hair_removal",
  "包茎": "phimosis",
  "包茎治療": "phimosis",
  "phimosis": "phimosis",
  "ダイエット": "diet",
  "医療ダイエット": "diet",
  "diet": "diet"
};
const SITE_LABEL_MAP = {
  "オーロラクリニック": "sites/aurora_clinic.json",
  "aurora_clinic": "sites/aurora_clinic.json",
  "明日のクリニック": "sites/ashitano_clinic.json",
  "ashitano_clinic": "sites/ashitano_clinic.json",
  "まめクリニック": "sites/mame_clinic.json",
  "mame_clinic": "sites/mame_clinic.json",
  "うつ予防": "sites/utu_yobo.json",
  "utu_yobo": "sites/utu_yobo.json"
};


// ========================================
// メニュー
// ========================================
function onOpen() {
  ensureSheetSetup();
  SpreadsheetApp.getUi()
    .createMenu("SEO記事生成")
    .addItem("シート初期化", "setupSheet")
    .addItem("選択行を実行", "runSelectedRow")
    .addItem("未実行を一括実行", "runAllPending")
    .addSeparator()
    .addItem("ステータス更新", "updateAllStatuses")
    .addItem("ステータストリガー設定", "installStatusTrigger")
    .addItem("ステータストリガー削除", "deleteStatusTriggers")
    .addItem("ヘルスチェック", "healthCheck")
    .addToUi();
}


function setupSheet() {
  ensureSheetSetup();
  SpreadsheetApp.getUi().alert("シート初期化が完了しました。ヘッダーとプルダウンを更新しました。");
}


function ensureSheetSetup() {
  var sheet = SpreadsheetApp.getActiveSheet();
  ensureHeaderRow(sheet);
  ensureColumnStyles(sheet);
  ensureDropdowns(sheet);
}


function ensureHeaderRow(sheet) {
  sheet.getRange(1, 1, 1, HEADER_VALUES[0].length).setValues(HEADER_VALUES);
  sheet.setFrozenRows(1);

  var headerRange = sheet.getRange(1, 1, 1, HEADER_VALUES[0].length);
  headerRange
    .setFontWeight("bold")
    .setBackground("#2f75b5")
    .setFontColor("#ffffff")
    .setHorizontalAlignment("center");
}


function ensureColumnStyles(sheet) {
  sheet.setColumnWidth(1, 180);
  sheet.setColumnWidth(2, 110);
  sheet.setColumnWidth(3, 210);
  sheet.setColumnWidth(4, 120);
  sheet.setColumnWidth(5, 120);
  sheet.setColumnWidth(6, 120);
  sheet.setColumnWidth(7, 150);
  sheet.setColumnWidth(8, 320);

  sheet.getRange(2, 7, Math.max(sheet.getMaxRows() - 1, 1), 1).setNumberFormat("yyyy-mm-dd hh:mm:ss");
}


function ensureDropdowns(sheet) {
  var rowCount = Math.max(sheet.getMaxRows() - 1, SETUP_ROW_COUNT);

  var genreRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(GENRE_OPTIONS, true)
    .setAllowInvalid(false)
    .setHelpText("ジャンルを選択してください")
    .build();

  var siteRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(SITE_OPTIONS, true)
    .setAllowInvalid(false)
    .setHelpText("投稿サイト設定ファイルを選択してください")
    .build();

  var statusRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUS_OPTIONS, true)
    .setAllowInvalid(false)
    .setHelpText("未実行・実行中・完了・エラーから選択してください")
    .build();

  sheet.getRange(2, 2, rowCount, 1).setDataValidation(genreRule);
  sheet.getRange(2, 3, rowCount, 1).setDataValidation(siteRule);
  sheet.getRange(2, 5, rowCount, 1).setDataValidation(statusRule);

  var notesRange = sheet.getRange(2, 3, rowCount, 1);
  notesRange.setNote(
    "サイト設定ファイルを選択してください。\n" +
    "例:\n" +
    "sites/aurora_clinic.json\n" +
    "sites/ashitano_clinic.json\n" +
    "sites/mame_clinic.json（投稿は自動スキップ）\n" +
    "sites/utu_yobo.json"
  );
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
  var genre = normalizeGenreValue(sheet.getRange(row, 2).getValue().toString().trim());
  var site = normalizeSiteValue(sheet.getRange(row, 3).getValue().toString().trim());
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

  sheet.getRange(row, 2).setValue(genre);

  // デフォルトサイト設定
  if (!site) {
    site = "sites/aurora_clinic.json";
  }

  sheet.getRange(row, 3).setValue(site);

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
    sheet.getRange(row, 8).setValue(response.message || "");

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
      var startedAt = response.started_at ? "\n開始: " + response.started_at : "";
      sheet.getRange(row, 8).setValue("実行中: " + (response.keyword || "") + startedAt);
      return;
    }

    if (jobStatus === "success") {
      sheet.getRange(row, 5).setValue("完了");
      sheet.getRange(row, 8).setValue("正常完了");
    } else {
      sheet.getRange(row, 5).setValue("エラー");
      sheet.getRange(row, 8).setValue(buildErrorMessage(response));
    }

    if (response.finished_at) {
      sheet.getRange(row, 7).setValue(new Date(response.finished_at));
    }

  } catch (e) {
    sheet.getRange(row, 5).setValue("エラー");
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
// トリガー設定
// ========================================

function installStatusTrigger() {
  deleteStatusTriggers();
  ScriptApp.newTrigger("updateAllStatuses")
    .timeBased()
    .everyMinutes(1)
    .create();
  SpreadsheetApp.getUi().alert("1分ごとのステータス更新トリガーを設定しました。");
}


function deleteStatusTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "updateAllStatuses") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
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
      "ngrok-skip-browser-warning": "true",
    },
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  var text = response.getContentText();
  var body;

  try {
    body = JSON.parse(text);
  } catch (e) {
    throw new Error("JSON以外の応答: " + text.substring(0, 200));
  }

  if (code >= 400) {
    throw new Error("HTTP " + code + ": " + (body.error || text));
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
      "ngrok-skip-browser-warning": "true",
    },
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  var text = response.getContentText();
  var body;

  try {
    body = JSON.parse(text);
  } catch (e) {
    throw new Error("JSON以外の応答: " + text.substring(0, 200));
  }

  if (code >= 400) {
    throw new Error("HTTP " + code + ": " + (body.error || text));
  }

  return body;
}


function buildErrorMessage(response) {
  var parts = ["結果: " + (response.status || "unknown")];

  if (response.error) {
    parts.push("エラー: " + response.error);
  }

  if (response.result && response.result.final_status) {
    parts.push("最終状態: " + response.result.final_status);
  }

  if (response.result && response.result.steps) {
    var failed = [];
    for (var stepName in response.result.steps) {
      var step = response.result.steps[stepName];
      if (step.status && step.status !== "ok" && step.status !== "skipped") {
        failed.push(stepName + " (" + step.status + ")");
      }
    }
    if (failed.length > 0) {
      parts.push("失敗ステップ: " + failed.join(", "));
    }
  }

  return parts.join("\n");
}


function normalizeSiteValue(site) {
  if (!site) {
    return "";
  }
  return SITE_LABEL_MAP[site] || site;
}


function normalizeGenreValue(genre) {
  if (!genre) {
    return "";
  }
  return GENRE_LABEL_MAP[genre] || genre;
}
