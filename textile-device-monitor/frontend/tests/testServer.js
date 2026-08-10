import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

export const server = setupServer(
  // 执行系统头部（ExecutionChrome）会在每个页面轮询待办角标；
  // 测试默认返回空待办，各用例可用 server.use 覆盖。
  http.get('/api/execution/v1/human-tasks', () => HttpResponse.json({ items: [] })),
);
