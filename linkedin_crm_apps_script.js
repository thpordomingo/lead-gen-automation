/**
 * INSTALLATION
 * 1. Open the Google Sheet.
 * 2. Go to Extensions > Apps Script.
 * 3. Paste this code into the Apps Script editor.
 * 4. Save the project.
 * 5. Run setupLinkedInCRM() once manually.
 * 6. Approve the requested permissions.
 * 7. Inside your existing onMessageLogged(e) trigger function, add this line:
 *    handleLinkedInReadyEdit(e);
 *
 * This file intentionally does not define a global onEdit(e).
 */

const LINKEDIN_READY_SHEET = 'LINKEDIN_READY';
const LINKEDIN_FOLLOW_UPS_SHEET = 'LINKEDIN_FOLLOW_UPS';

const LINKEDIN_DROPDOWNS = {
  Status: [
    'New',
    'Connection Sent',
    'Connected',
    'Message Sent',
    'Replied',
    'Booked',
    'Not Interested',
    'Bad Fit',
    'Nurture',
  ],
  Last_Touch_Type: [
    'Connection Sent',
    'Connection Accepted',
    'Message Sent',
    'Follow-Up Sent',
    'Replied',
    'Booked',
    'Not Interested',
    'Bad Fit',
  ],
  Next_Action: [
    'Check Connection',
    'Send Message',
    'Send Follow-Up',
    'Reply',
    'Book Meeting',
    'Nurture',
    'None',
  ],
  Follow_Up_Status: [
    'Due Today',
    'Upcoming',
    'Overdue',
    'Done',
    'Paused',
  ],
};

const LINKEDIN_RULES = {
  'Connection Sent': {
    nextAction: 'Check Connection',
    nextTouchOffsetDays: 3,
    status: 'Connection Sent',
  },
  'Connection Accepted': {
    nextAction: 'Send Message',
    nextTouchOffsetDays: 0,
    status: 'Connected',
  },
  'Message Sent': {
    nextAction: 'Send Follow-Up',
    nextTouchOffsetDays: 3,
    status: 'Message Sent',
  },
  'Follow-Up Sent': {
    nextAction: 'Nurture',
    nextTouchOffsetDays: 7,
    status: 'Message Sent',
  },
  'Replied': {
    nextAction: 'Reply',
    nextTouchOffsetDays: 0,
    status: 'Replied',
  },
  'Booked': {
    nextAction: 'None',
    nextTouchOffsetDays: null,
    status: 'Booked',
  },
  'Not Interested': {
    nextAction: 'Nurture',
    nextTouchOffsetDays: 30,
    status: 'Not Interested',
  },
  'Bad Fit': {
    nextAction: 'None',
    nextTouchOffsetDays: null,
    status: 'Bad Fit',
  },
};

function setupLinkedInCRM() {
  const sheet = linkedinGetRequiredSheet_(LINKEDIN_READY_SHEET);
  const headers = linkedinGetHeaderMap_(sheet);
  const maxRows = Math.max(sheet.getMaxRows(), 2);

  Object.keys(LINKEDIN_DROPDOWNS).forEach((headerName) => {
    const col = linkedinGetRequiredColumn_(headers, headerName);
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(LINKEDIN_DROPDOWNS[headerName], true)
      .setAllowInvalid(false)
      .build();
    sheet.getRange(2, col, maxRows - 1, 1).setDataValidation(rule);
  });
}

function handleLinkedInReadyEdit(e) {
  if (!e || !e.range) {
    return;
  }

  const sheet = e.range.getSheet();
  if (sheet.getName() !== LINKEDIN_READY_SHEET) {
    return;
  }

  const row = e.range.getRow();
  if (row === 1) {
    return;
  }

  const headers = linkedinGetHeaderMap_(sheet);
  const lastTouchTypeCol = linkedinGetRequiredColumn_(headers, 'Last_Touch_Type');
  if (e.range.getColumn() !== lastTouchTypeCol) {
    return;
  }

  const touchType = String(sheet.getRange(row, lastTouchTypeCol).getValue()).trim();
  if (!touchType || !LINKEDIN_RULES[touchType]) {
    return;
  }

  const today = linkedinStartOfDay_(new Date());
  const rule = LINKEDIN_RULES[touchType];
  const updates = {};

  updates[linkedinGetRequiredColumn_(headers, 'Last_Touch_Date')] = today;

  const touchCountCol = linkedinGetRequiredColumn_(headers, 'Touch_Count');
  const currentTouchCount = Number(sheet.getRange(row, touchCountCol).getValue()) || 0;
  updates[touchCountCol] = currentTouchCount + 1;

  updates[linkedinGetRequiredColumn_(headers, 'Next_Action')] = rule.nextAction;
  updates[linkedinGetRequiredColumn_(headers, 'Status')] = rule.status;

  const nextTouchDateCol = linkedinGetRequiredColumn_(headers, 'Next_Touch_Date');
  const nextTouchDate = rule.nextTouchOffsetDays === null
    ? ''
    : linkedinAddDays_(today, rule.nextTouchOffsetDays);
  updates[nextTouchDateCol] = nextTouchDate;

  const followUpStatusCol = linkedinGetRequiredColumn_(headers, 'Follow_Up_Status');
  updates[followUpStatusCol] = linkedinDeriveFollowUpStatus_(nextTouchDate, today);

  linkedinWriteRowUpdates_(sheet, row, updates);
}

function refreshLinkedInFollowUps() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sourceSheet = linkedinGetRequiredSheet_(LINKEDIN_READY_SHEET);
  const targetSheet = ss.getSheetByName(LINKEDIN_FOLLOW_UPS_SHEET) || ss.insertSheet(LINKEDIN_FOLLOW_UPS_SHEET);
  const headers = linkedinGetHeaderMap_(sourceSheet);
  const allValues = sourceSheet.getDataRange().getValues();

  const outputHeaders = [
    'Lead_ID',
    'Owner_Name',
    'Title',
    'Company_Name',
    'LinkedIn_Profile_URL',
    'City',
    'State',
    'Service_Category',
    'Status',
    'Next_Action',
    'Last_Touch_Date',
    'Next_Touch_Date',
    'Touch_Count',
    'Notes',
  ];

  const today = linkedinStartOfDay_(new Date());
  const outputRows = [outputHeaders];

  for (let i = 1; i < allValues.length; i += 1) {
    const row = allValues[i];
    const status = String(linkedinGetCellByHeader_(row, headers, 'Status')).trim();
    const nextAction = String(linkedinGetCellByHeader_(row, headers, 'Next_Action')).trim();
    const nextTouchDate = linkedinNormalizeSheetDate_(linkedinGetCellByHeader_(row, headers, 'Next_Touch_Date'));

    if (!nextTouchDate) {
      continue;
    }
    if (nextTouchDate.getTime() > today.getTime()) {
      continue;
    }
    if (status === 'Booked' || status === 'Bad Fit') {
      continue;
    }
    if (nextAction === 'None') {
      continue;
    }

    outputRows.push(outputHeaders.map((header) => linkedinGetCellByHeader_(row, headers, header)));
  }

  targetSheet.clear();
  targetSheet.getRange(1, 1, outputRows.length, outputHeaders.length).setValues(outputRows);
}

function linkedinGetRequiredSheet_(sheetName) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(sheetName);
  if (!sheet) {
    throw new Error(`Worksheet not found: ${sheetName}`);
  }
  return sheet;
}

function linkedinGetHeaderMap_(sheet) {
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

function linkedinGetRequiredColumn_(headers, headerName) {
  const col = headers[headerName];
  if (!col) {
    throw new Error(`Missing required header: ${headerName}`);
  }
  return col;
}

function linkedinGetCellByHeader_(rowValues, headers, headerName) {
  const col = linkedinGetRequiredColumn_(headers, headerName);
  return rowValues[col - 1];
}

function linkedinWriteRowUpdates_(sheet, row, updates) {
  const cols = Object.keys(updates)
    .map(Number)
    .sort((a, b) => a - b);

  cols.forEach((col) => {
    sheet.getRange(row, col).setValue(updates[col]);
  });
}

function linkedinStartOfDay_(date) {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy;
}

function linkedinAddDays_(date, days) {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + days);
  return copy;
}

function linkedinNormalizeSheetDate_(value) {
  if (!value) {
    return null;
  }
  const date = value instanceof Date ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return linkedinStartOfDay_(date);
}

function linkedinDeriveFollowUpStatus_(nextTouchDate, today) {
  if (!nextTouchDate) {
    return 'Done';
  }

  const normalizedNextTouchDate = linkedinStartOfDay_(nextTouchDate);
  const normalizedToday = linkedinStartOfDay_(today || new Date());

  if (normalizedNextTouchDate.getTime() < normalizedToday.getTime()) {
    return 'Overdue';
  }
  if (normalizedNextTouchDate.getTime() === normalizedToday.getTime()) {
    return 'Due Today';
  }
  return 'Upcoming';
}
