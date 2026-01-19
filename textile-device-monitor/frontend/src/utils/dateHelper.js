import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

export const formatDateTime = (datetime) => {
  if (!datetime) return '-';
  return dayjs(datetime).format('YYYY-MM-DD HH:mm:ss');
};

export const formatTime = (datetime) => {
  if (!datetime) return '-';
  return dayjs(datetime).format('HH:mm:ss');
};

export const formatRelativeTime = (datetime) => {
  if (!datetime) return '-';
  return dayjs(datetime).fromNow();
};

export const formatDate = (date) => {
  if (!date) return '-';
  return dayjs(date).format('YYYY-MM-DD');
};
