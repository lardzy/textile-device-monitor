import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import timezone from 'dayjs/plugin/timezone';
import utc from 'dayjs/plugin/utc';
import 'dayjs/locale/zh-cn';

dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

export const DISPLAY_TIMEZONE = 'Asia/Shanghai';

export const toDisplayDayjs = (value) => dayjs(value).tz(DISPLAY_TIMEZONE);

export const displayNow = () => dayjs().tz(DISPLAY_TIMEZONE);

export const parseDisplayDate = (value) => {
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return dayjs.tz(value, DISPLAY_TIMEZONE);
  }
  return toDisplayDayjs(value);
};

export const formatDateTime = (datetime) => {
  if (!datetime) return '-';
  return toDisplayDayjs(datetime).format('YYYY-MM-DD HH:mm:ss');
};

export const formatTime = (datetime) => {
  if (!datetime) return '-';
  return toDisplayDayjs(datetime).format('HH:mm:ss');
};

export const formatRelativeTime = (datetime) => {
  if (!datetime) return '-';
  return toDisplayDayjs(datetime).fromNow();
};

export const formatDate = (date) => {
  if (!date) return '-';
  return toDisplayDayjs(date).format('YYYY-MM-DD');
};
