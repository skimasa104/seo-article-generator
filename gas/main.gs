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
 *   E列: ステータス（自動更新: 未実行 / 待機中 / 実行中 / 完了 / エラー）
 *   F列: ジョブID（自動記入）
 *   G列: 実行日時（自動記入）
 *   H列: 備考（自動記入: エラー内容等）
 *   I列: 生成数（なんでも選択時のみ使用: 1〜5）
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
  "備考",
  "生成数",
  "参考URL"
]];

const GENRE_OPTIONS = [
  "AGA",
  "ED",
  "医療脱毛",
  "包茎",
  "ダイエット"
];
const SITE_OPTIONS = [
  "オーロラクリニック",
  "明日のクリニック",
  "まめクリニック",
  "うつ予防",
  "なんでも"
];
const STATUS_OPTIONS = ["未実行", "待機中", "実行中", "完了", "エラー"];
const VARIANT_COUNT_OPTIONS = ["1", "2", "3", "4", "5"];
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
const GENRE_DISPLAY_MAP = {
  "aga": "AGA",
  "ed": "ED",
  "hair_removal": "医療脱毛",
  "phimosis": "包茎",
  "diet": "ダイエット"
};
const SITE_LABEL_MAP = {
  "オーロラクリニック": "sites/aurora_clinic.json",
  "aurora_clinic": "sites/aurora_clinic.json",
  "sites/aurora_clinic.json": "sites/aurora_clinic.json",
  "aurora-clinic.jp": "sites/aurora_clinic.json",
  "明日のクリニック": "sites/ashitano_clinic.json",
  "ashitano_clinic": "sites/ashitano_clinic.json",
  "sites/ashitano_clinic.json": "sites/ashitano_clinic.json",
  "ashitano.clinic": "sites/ashitano_clinic.json",
  "まめクリニック": "sites/mame_clinic.json",
  "mame_clinic": "sites/mame_clinic.json",
  "sites/mame_clinic.json": "sites/mame_clinic.json",
  "mame-clinic.net": "sites/mame_clinic.json",
  "うつ予防": "sites/utu_yobo.json",
  "utu_yobo": "sites/utu_yobo.json",
  "sites/utu_yobo.json": "sites/utu_yobo.json",
  "utu-yobo.com": "sites/utu_yobo.json",
  "なんでも": "sites/nandemo.json",
  "nandemo": "sites/nandemo.json",
  "sites/nandemo.json": "sites/nandemo.json",
  "nandemo.trigger-tech.info": "sites/nandemo.json"
};
const SITE_DISPLAY_MAP = {
  "sites/aurora_clinic.json": "オーロラクリニック",
  "sites/ashitano_clinic.json": "明日のクリニック",
  "sites/mame_clinic.json": "まめクリニック",
  "sites/utu_yobo.json": "うつ予防",
  "sites/nandemo.json": "なんでも"
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
    .addItem("選択行を未実行に戻す", "resetSelectedRowsToPending")
    .addItem("選択行のステータス再判定", "reconcileSelectedRows")
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


function onEdit(e) {
  if (!e || !e.range) {
    return;
  }

  var range = e.range;
  var sheet = range.getSheet();
  var row = range.getRow();
  var col = range.getColumn();

  if (row <= 1) {
    return;
  }

  if ((col >= 1 && col <= 4) || col === 9 || col === 10) {
    invalidateRowIfCoreFieldsChanged(sheet, row);
  }
}


function ensureSheetSetup() {
  var sheet = SpreadsheetApp.getActiveSheet();
  ensureHeaderRow(sheet);
  ensureColumnStyles(sheet);
  ensureDropdowns(sheet);
  normalizeExistingSelections(sheet);
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
  sheet.setColumnWidth(9, 100);
  sheet.setColumnWidth(10, 280);

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
    .setHelpText("未実行・待機中・実行中・完了・エラーから選択してください")
    .build();

  var variantCountRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(VARIANT_COUNT_OPTIONS, true)
    .setAllowInvalid(false)
    .setHelpText("なんでも向けの生成数を1〜5で指定してください")
    .build();

  sheet.getRange(2, 2, rowCount, 1).setDataValidation(genreRule);
  sheet.getRange(2, 3, rowCount, 1).setDataValidation(siteRule);
  sheet.getRange(2, 5, rowCount, 1).setDataValidation(statusRule);
  sheet.getRange(2, 9, rowCount, 1).setDataValidation(variantCountRule);

  var notesRange = sheet.getRange(2, 3, rowCount, 1);
  notesRange.setNote(
    "投稿サイトを選択してください。\n" +
    "サイト名でも設定ファイル名でも選べます。\n" +
    "例:\n" +
    "オーロラクリニック / sites/aurora_clinic.json\n" +
    "明日のクリニック / sites/ashitano_clinic.json\n" +
    "まめクリニック / sites/mame_clinic.json（投稿は自動スキップ）\n" +
    "うつ予防 / sites/utu_yobo.json\n" +
    "なんでも / sites/nandemo.json"
  );

  sheet.getRange(2, 9, rowCount, 1).setNote(
    "生成数を指定してください。\n" +
    "通常サイトは 1 のままでOKです。\n" +
    "なんでも を選んだ場合のみ、1〜5本の別編集版を生成できます。"
  );

  sheet.getRange(2, 10, rowCount, 1).setNote(
    "（任意）構造を参照したい競合記事のURLを貼り付けます。\n" +
    "指定するとそのURLが Step 0 検索結果の先頭に強制配置され、\n" +
    "Step 2 のタグ構成設計では「1位記事」として構造（H2/H3 階層・文字数・ブロック型）を踏襲します。\n" +
    "本文は完全に独自表現で書き起こされます（コピーや言い換えはしません）。\n" +
    "空欄の場合は Google検索1位を自動で参照します。"
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
    if (status === "未実行") {
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
 * 選択中の行を未実行に戻す
 * ステータス・ジョブID・実行日時・備考をクリアする
 */
function resetSelectedRowsToPending() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getActiveRange();

  if (!range) {
    SpreadsheetApp.getUi().alert("リセットする行を選択してください。");
    return;
  }

  var startRow = range.getRow();
  var numRows = range.getNumRows();
  var resetCount = 0;

  for (var offset = 0; offset < numRows; offset++) {
    var row = startRow + offset;
    if (row <= 1) {
      continue;
    }
    resetRowState(sheet, row);
    resetCount++;
  }

  if (resetCount === 0) {
    SpreadsheetApp.getUi().alert("2行目以降を選択してください。");
    return;
  }

  SpreadsheetApp.getUi().alert(resetCount + "行を未実行に戻しました。");
}


function reconcileSelectedRows() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getActiveRange();

  if (!range) {
    SpreadsheetApp.getUi().alert("再判定する行を選択してください。");
    return;
  }

  var startRow = range.getRow();
  var numRows = range.getNumRows();
  var checkedCount = 0;

  for (var offset = 0; offset < numRows; offset++) {
    var row = startRow + offset;
    if (row <= 1) {
      continue;
    }
    reconcileRowStatus(sheet, row);
    checkedCount++;
  }

  if (checkedCount === 0) {
    SpreadsheetApp.getUi().alert("2行目以降を選択してください。");
    return;
  }

  SpreadsheetApp.getUi().alert(checkedCount + "行のステータスを再判定しました。");
}


/**
 * 指定行のパイプラインを実行
 */
function runRow(sheet, row) {
  var keyword = sheet.getRange(row, 1).getValue().toString().trim();
  var genreRaw = sheet.getRange(row, 2).getValue().toString().trim();
  var siteRaw = sheet.getRange(row, 3).getValue().toString().trim();
  var genre = normalizeGenreValue(genreRaw);
  var site = normalizeSiteValue(siteRaw);
  var category = sheet.getRange(row, 4).getValue().toString().trim();
  var variantCountRaw = sheet.getRange(row, 9).getValue().toString().trim();
  var variantCount = parseInt(variantCountRaw || "1", 10);
  var referenceUrl = sheet.getRange(row, 10).getValue().toString().trim();

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

  sheet.getRange(row, 2).setValue(toGenreDisplayValue(genreRaw || genre));

  // デフォルトサイト設定
  if (!site) {
    site = "sites/aurora_clinic.json";
  }

  sheet.getRange(row, 3).setValue(toSiteDisplayValue(siteRaw || site));

  if (!variantCount || variantCount < 1) {
    variantCount = 1;
  }
  if (site !== "sites/nandemo.json") {
    variantCount = 1;
  }
  if (variantCount > 5) {
    variantCount = 5;
  }
  sheet.getRange(row, 9).setValue(String(variantCount));

  // リクエスト送信
  var payload = {
    keyword: keyword,
    genre: genre,
    site: site,
    category: category,
    variant_count: variantCount,
    reference_url: referenceUrl,
  };

  try {
    var response = postRequest("/run", payload);

    if (response.error) {
      sheet.getRange(row, 5).setValue("エラー");
      sheet.getRange(row, 8).setValue(response.error);
      return;
    }

    // 成功: ジョブ情報を記入
    if (response.phase === "queued") {
      sheet.getRange(row, 5).setValue("待機中");
    } else {
      sheet.getRange(row, 5).setValue("実行中");
    }
    sheet.getRange(row, 6).setValue(response.job_id);
    sheet.getRange(row, 7).setValue(new Date());
    sheet.getRange(row, 8).setValue(response.message || "");

  } catch (e) {
    sheet.getRange(row, 5).setValue("エラー");
    sheet.getRange(row, 8).setValue("接続エラー: " + e.message);
  }
}


function resetRowState(sheet, row) {
  sheet.getRange(row, 5).setValue("未実行");
  sheet.getRange(row, 6).clearContent();
  sheet.getRange(row, 7).clearContent();
  sheet.getRange(row, 8).clearContent();
}


function invalidateRowIfCoreFieldsChanged(sheet, row) {
  var currentStatus = sheet.getRange(row, 5).getValue().toString().trim();
  var currentJobId = sheet.getRange(row, 6).getValue().toString().trim();
  var currentNote = sheet.getRange(row, 8).getValue().toString().trim();

  if (!currentStatus && !currentJobId && !currentNote) {
    return;
  }

  if (currentStatus === "未実行" && !currentJobId) {
    return;
  }

  resetRowState(sheet, row);
}


// ========================================
// ステータス確認
// ========================================

/**
 * 「待機中」「実行中」の全行のステータスを更新
 */
function updateAllStatuses() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var lastRow = sheet.getLastRow();
  var updated = 0;

  for (var row = 2; row <= lastRow; row++) {
    var status = sheet.getRange(row, 5).getValue();
    if (status === "実行中" || status === "待機中") {
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
      reconcileRowStatus(sheet, row);
      return;
    }

    var jobStatus = response.status;

    if (jobStatus === "running") {
      var phase = response.phase || "";
      var queuePosition = response.queue_position || 0;
      var startedAt = response.started_at ? "\n開始: " + response.started_at : "";

      if (phase === "queued") {
        sheet.getRange(row, 5).setValue("待機中");
        sheet.getRange(row, 8).setValue(
          "待機中: " + (response.keyword || "") +
          (queuePosition > 0 ? "\nキュー: " + queuePosition + "番目" : "") +
          startedAt
        );
      } else {
        sheet.getRange(row, 5).setValue("実行中");
        sheet.getRange(row, 8).setValue("実行中: " + (response.keyword || "") + startedAt);
      }
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
    reconcileRowStatus(sheet, row);
  }
}


function reconcileRowStatus(sheet, row) {
  var keyword = sheet.getRange(row, 1).getValue().toString().trim();
  var jobId = sheet.getRange(row, 6).getValue().toString().trim();

  if (!keyword && !jobId) {
    resetRowState(sheet, row);
    return;
  }

  try {
    var path = "/reconcile?keyword=" + encodeURIComponent(keyword);
    if (jobId) {
      path += "&job_id=" + encodeURIComponent(jobId);
    }
    var response = getRequest(path);

    if (response.error) {
      sheet.getRange(row, 5).setValue("エラー");
      sheet.getRange(row, 8).setValue("再判定エラー: " + response.error);
      return;
    }

    applyReconciledStatus(sheet, row, response);
  } catch (e) {
    sheet.getRange(row, 5).setValue("エラー");
    sheet.getRange(row, 8).setValue("再判定エラー: " + e.message);
  }
}


function applyReconciledStatus(sheet, row, response) {
  var status = response.status || "";
  var phase = response.phase || "";
  var message = response.message || "";
  var queuePosition = response.queue_position || 0;
  var startedAt = response.started_at;
  var finishedAt = response.finished_at;

  if (response.id) {
    sheet.getRange(row, 6).setValue(response.id);
  }

  if (status === "running") {
    if (phase === "queued") {
      sheet.getRange(row, 5).setValue("待機中");
      sheet.getRange(row, 8).setValue(
        "待機中: " + (response.keyword || "") +
        (queuePosition > 0 ? "\nキュー: " + queuePosition + "番目" : "") +
        (startedAt ? "\n開始: " + startedAt : "")
      );
    } else {
      sheet.getRange(row, 5).setValue("実行中");
      sheet.getRange(row, 8).setValue(
        "実行中: " + (response.keyword || "") +
        (startedAt ? "\n開始: " + startedAt : "")
      );
    }
    if (startedAt) {
      sheet.getRange(row, 7).setValue(new Date(startedAt));
    }
    return;
  }

  if (status === "success") {
    sheet.getRange(row, 5).setValue("完了");
    sheet.getRange(row, 8).setValue("正常完了");
    if (finishedAt) {
      sheet.getRange(row, 7).setValue(new Date(finishedAt));
    }
    return;
  }

  if (status === "not_found") {
    resetRowState(sheet, row);
    return;
  }

  sheet.getRange(row, 5).setValue("エラー");
  if (finishedAt) {
    sheet.getRange(row, 7).setValue(new Date(finishedAt));
  }
  if (response.result) {
    sheet.getRange(row, 8).setValue(buildErrorMessage(response));
  } else {
    sheet.getRange(row, 8).setValue(message || "エラー");
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


function toGenreDisplayValue(value) {
  var normalized = normalizeGenreValue(value);
  return GENRE_DISPLAY_MAP[normalized] || value;
}


function toSiteDisplayValue(value) {
  var normalized = normalizeSiteValue(value);
  return SITE_DISPLAY_MAP[normalized] || value;
}


function normalizeExistingSelections(sheet) {
  var lastRow = Math.max(sheet.getLastRow(), 2);
  if (lastRow < 2) {
    return;
  }

  var genreRange = sheet.getRange(2, 2, lastRow - 1, 1);
  var genreValues = genreRange.getValues();
  for (var i = 0; i < genreValues.length; i++) {
    var currentGenre = genreValues[i][0].toString().trim();
    if (!currentGenre) {
      continue;
    }
    genreValues[i][0] = toGenreDisplayValue(currentGenre);
  }
  genreRange.setValues(genreValues);

  var siteRange = sheet.getRange(2, 3, lastRow - 1, 1);
  var siteValues = siteRange.getValues();
  for (var j = 0; j < siteValues.length; j++) {
    var currentSite = siteValues[j][0].toString().trim();
    if (!currentSite) {
      continue;
    }
    siteValues[j][0] = toSiteDisplayValue(currentSite);
  }
  siteRange.setValues(siteValues);
}
