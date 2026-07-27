(function customerHubV2FollowupDateModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else if (root && typeof root === "object") {
    root.CustomerHubV2FollowupDate = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function createCustomerHubV2FollowupDate() {
  "use strict";

  function daysInMonth(year, month) {
    if (month === 2) {
      const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
      return leap ? 29 : 28;
    }
    return [4, 6, 9, 11].includes(month) ? 30 : 31;
  }

  function invalidFollowupBaseDate() {
    throw new TypeError("invalid_followup_base_date");
  }

  function computeNextFollowupDate(baseDateTime, preset) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T([01]\d|2[0-3]):([0-5]\d)$/.exec(baseDateTime);
    if (!match) invalidFollowupBaseDate();

    let year = Number(match[1]);
    let month = Number(match[2]);
    let day = Number(match[3]);
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
      invalidFollowupBaseDate();
    }

    if (preset === "15d") {
      day += 15;
      while (day > daysInMonth(year, month)) {
        day -= daysInMonth(year, month);
        month += 1;
        if (month === 13) {
          month = 1;
          year += 1;
        }
      }
    } else if (preset === "1m" || preset === "6m") {
      const offset = preset === "1m" ? 1 : 6;
      const total = year * 12 + month - 1 + offset;
      year = Math.floor(total / 12);
      month = total % 12 + 1;
      day = Math.min(day, daysInMonth(year, month));
    } else if (preset === "1y") {
      year += 1;
      day = Math.min(day, daysInMonth(year, month));
    } else {
      invalidFollowupBaseDate();
    }

    if (year > 9999) invalidFollowupBaseDate();
    const pad = value => String(value).padStart(2, "0");
    return `${String(year).padStart(4, "0")}-${pad(month)}-${pad(day)}`;
  }

  return Object.freeze({computeNextFollowupDate});
});
