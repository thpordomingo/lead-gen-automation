/**
 * INSTALLATION
 * 1. Open the Google Sheet.
 * 2. Go to Extensions > Apps Script.
 * 3. Paste this code into the Apps Script editor.
 * 4. Save the project.
 * 5. Run setupCallReadyCRM() once manually.
 * 6. Approve the requested permissions.
 * 7. After that, onEdit(e) will run automatically on edits in CALL_READY.
 */

const CALL_READY_SHEET = 'CALL_READY';
const DAILY_FOLLOW_UPS_SHEET = 'DAILY_FOLLOW_UPS';

const DROPDOWNS = {
  Last_Touch_Channel: [
    'Phone',
    'SMS',
    'Facebook',
    'LinkedIn',
    'Email',
    'Other',
  ],
  Last_Touch_Outcome: [
    'No Answer',
    'Voicemail Left',
    'SMS Sent',
    'Gatekeeper',
    'Connected - Interested',
    'Connected - Send Info',
    'Connected - Follow Up Later',
    'Connected - Not Interested',
    'Booked',
    'Bad Number',
    'Disqualified',
  ],
  Next_Action: [
    'Call Again',
    'Send SMS',
    'Follow Up',
    'Book Meeting',
    'LinkedIn Touch',
    'Facebook Touch',
    'Nurture',
    'None',
  ],
  Next_Touch_Channel: [
    'Phone',
    'SMS',
    'Facebook',
    'LinkedIn',
    'None',
  ],
  Follow_Up_Status: [
    'Due Today',
    'Upcoming',
    'Overdue',
    'Done',
    'Paused',
  ],
  LinkedIn_Status: [
    'Not Found',
    'Found',
    'Connection Sent',
    'Connected',
    'Message Sent',
    'Replied',
    'Not Relevant',
  ],
  Facebook_Status: [
    'Not Found',
    'Found',
    'Message Sent',
    'Replied',
    'Not Relevant',
  ],
};

const FOLLOW_UP_RULES = {
  'No Answer': {
    nextAction: 'Send SMS',
    nextTouchChannel: 'SMS',
    nextTouchOffsetDays: 0,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'Voicemail Left': {
    nextAction: 'Send SMS',
    nextTouchChannel: 'SMS',
    nextTouchOffsetDays: 0,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'SMS Sent': {
    nextAction: 'Call Again',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 2,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'Gatekeeper': {
    nextAction: 'Call Again',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 2,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'Connected - Send Info': {
    nextAction: 'Follow Up',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 2,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'Connected - Follow Up Later': {
    nextAction: 'Follow Up',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 7,
    callStatus: 'Follow-Up',
    meetingBooked: null,
  },
  'Connected - Interested': {
    nextAction: 'Book Meeting',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 0,
    callStatus: 'Interested',
    meetingBooked: null,
  },
  'Booked': {
    nextAction: 'None',
    nextTouchChannel: 'None',
    nextTouchOffsetDays: null,
    callStatus: 'Booked',
    meetingBooked: 'Yes',
  },
  'Bad Number': {
    nextAction: 'None',
    nextTouchChannel: 'None',
    nextTouchOffsetDays: null,
    callStatus: 'Bad Number',
    meetingBooked: null,
  },
  'Connected - Not Interested': {
    nextAction: 'Nurture',
    nextTouchChannel: 'Phone',
    nextTouchOffsetDays: 30,
    callStatus: 'Nurture',
    meetingBooked: null,
  },
  'Disqualified': {
    nextAction: 'None',
    nextTouchChannel: 'None',
    nextTouchOffsetDays: null,
    callStatus: 'Disqualified',
    meetingBooked: null,
  },
};

function setupCallReadyCRM() {
  const sheet = getRequiredSheet_(CALL_READY_SHEET);
  const headers = getHeaderMap_(sheet);
  const lastRow = Math.max(sheet.getMaxRows(), 2);

  Object.keys(DROPDOWNS).forEach((headerName) => {
    const col = getRequiredColumn_(headers, headerName);
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(DROPDOWNS[headerName], true)
      .setAllowInvalid(false)
      .build();
    sheet.getRange(2, col, lastRow - 1, 1).setDataValidation(rule);
  });
}

function onEdit(e) {
  if (!e || !e.range) {
    return;
  }

  const sheet = e.range.getSheet();
  if (sheet.getName() !== CALL_READY_SHEET) {
    return;
  }

  const row = e.range.getRow();
  if (row === 1) {
    return;
  }

  const headers = getHeaderMap_(sheet);
  const watchedColumns = [
    getRequiredColumn_(headers, 'Last_Touch_Channel'),
    getRequiredColumn_(headers, 'Last_Touch_Outcome'),
  ];

  if (!watchedColumns.includes(e.range.getColumn())) {
    return;
  }

  const outcomeCol = getRequiredColumn_(headers, 'Last_Touch_Outcome');
  const outcome = String(sheet.getRange(row, outcomeCol).getValue()).trim();
  if (!outcome || !FOLLOW_UP_RULES[outcome]) {
    return;
  }

  const today = startOfDay_(new Date());
  const rule = FOLLOW_UP_RULES[outcome];

  const updates = {};
  updates[getRequiredColumn_(headers, 'Last_Touch_Date')] = today;

  const touchCountCol = getRequiredColumn_(headers, 'Touch_Count');
  const currentTouchCount = Number(sheet.getRange(row, touchCountCol).getValue()) || 0;
  updates[touchCountCol] = currentTouchCount + 1;

  updates[getRequiredColumn_(headers, 'Next_Action')] = rule.nextAction;
  updates[getRequiredColumn_(headers, 'Next_Touch_Channel')] = rule.nextTouchChannel;
  updates[getRequiredColumn_(headers, 'Call_Status')] = rule.callStatus;

  const nextTouchDateCol = getRequiredColumn_(headers, 'Next_Touch_Date');
  const nextTouchDate = rule.nextTouchOffsetDays === null
    ? ''
    : addDays_(today, rule.nextTouchOffsetDays);
  updates[nextTouchDateCol] = nextTouchDate;

  const meetingBookedCol = getRequiredColumn_(headers, 'Meeting_Booked');
  if (rule.meetingBooked !== null) {
    updates[meetingBookedCol] = rule.meetingBooked;
  }

  const followUpStatusCol = getRequiredColumn_(headers, 'Follow_Up_Status');
  updates[followUpStatusCol] = deriveFollowUpStatus_(nextTouchDate, today);

  writeRowUpdates_(sheet, row, updates);
}

function refreshDailyFollowUps() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sourceSheet = getRequiredSheet_(CALL_READY_SHEET);
  const targetSheet = ss.getSheetByName(DAILY_FOLLOW_UPS_SHEET) || ss.insertSheet(DAILY_FOLLOW_UPS_SHEET);
  const headers = getHeaderMap_(sourceSheet);
  const allValues = sourceSheet.getDataRange().getValues();

  const outputHeaders = [
    'Lead_ID',
    'Company_Name',
    'Owner_Name',
    'Phone',
    'City',
    'State',
    'Service_Category',
    'Fit_Score',
    'Call_Status',
    'Next_Action',
    'Next_Touch_Channel',
    'Last_Touch_Date',
    'Next_Touch_Date',
    'Touch_Count',
    'Call_Notes',
    'LinkedIn_Profile_URL',
    'Facebook_URL',
  ];

  const today = startOfDay_(new Date());
  const rows = [outputHeaders];

  for (let i = 1; i < allValues.length; i += 1) {
    const row = allValues[i];
    const callStatus = String(getCellByHeader_(row, headers, 'Call_Status')).trim();
    const nextAction = String(getCellByHeader_(row, headers, 'Next_Action')).trim();
    const nextTouchDateRaw = getCellByHeader_(row, headers, 'Next_Touch_Date');
    const nextTouchDate = normalizeSheetDate_(nextTouchDateRaw);

    if (!nextTouchDate) {
      continue;
    }
    if (nextTouchDate.getTime() > today.getTime()) {
      continue;
    }
    if (['Booked', 'Disqualified', 'Bad Number'].includes(callStatus)) {
      continue;
    }
    if (nextAction === 'None') {
      continue;
    }

    rows.push(outputHeaders.map((header) => getCellByHeader_(row, headers, header)));
  }

  targetSheet.clear();
  targetSheet.getRange(1, 1, rows.length, outputHeaders.length).setValues(rows);
}

function getRequiredSheet_(sheetName) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(sheetName);
  if (!sheet) {
    throw new Error(`Worksheet not found: ${sheetName}`);
  }
  return sheet;
}

function getHeaderMap_(sheet) {
  const headerValues = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const map = {};
  headerValues.forEach((header, index) => {
    const headerName = String(header).trim();
    if (headerName) {
      map[headerName] = index + 1;
    }
  });
  return map;
}

function getRequiredColumn_(headers, headerName) {
  const col = headers[headerName];
  if (!col) {
    throw new Error(`Missing required header: ${headerName}`);
  }
  return col;
}

function getCellByHeader_(rowValues, headers, headerName) {
  const col = getRequiredColumn_(headers, headerName);
  return rowValues[col - 1];
}

function writeRowUpdates_(sheet, row, updates) {
  const columns = Object.keys(updates)
    .map(Number)
    .sort((a, b) => a - b);

  columns.forEach((col) => {
    sheet.getRange(row, col).setValue(updates[col]);
  });
}

function startOfDay_(date) {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy;
}

function addDays_(date, days) {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + days);
  return copy;
}

function normalizeSheetDate_(value) {
  if (!value) {
    return null;
  }
  const date = value instanceof Date ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return startOfDay_(date);
}

function deriveFollowUpStatus_(nextTouchDate, today) {
  if (!nextTouchDate) {
    return 'Done';
  }

  const normalizedNextTouchDate = startOfDay_(nextTouchDate);
  const normalizedToday = startOfDay_(today || new Date());

  if (normalizedNextTouchDate.getTime() < normalizedToday.getTime()) {
    return 'Overdue';
  }
  if (normalizedNextTouchDate.getTime() === normalizedToday.getTime()) {
    return 'Due Today';
  }
  return 'Upcoming';
}
