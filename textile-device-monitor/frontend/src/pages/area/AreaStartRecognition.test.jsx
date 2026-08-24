import {
  act,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  Route,
  Routes,
} from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { areaApi } from '../../api/area';
import AreaStartRecognition from './AreaStartRecognition';

vi.mock('../../api/area', () => ({
  areaApi: {
    getConfig: vi.fn(),
    getStatus: vi.fn(),
    listRecentFolders: vi.fn(),
    searchFolders: vi.fn(),
    listFolderPreviewImages: vi.fn(),
    getFolderImageUrl: vi.fn(),
    createJob: vi.fn(),
    previewFolderCleanup: vi.fn(),
    cleanupFolder: vi.fn(),
  },
}));

const renderStartPage = () => render(
  <MemoryRouter
    initialEntries={['/tools/area']}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <Routes>
      <Route path="/tools/area" element={<AreaStartRecognition />} />
      <Route path="/tools/area/tasks" element={<div>任务记录</div>} />
    </Routes>
  </MemoryRouter>,
);

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

describe('AreaStartRecognition', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    areaApi.getConfig.mockResolvedValue({ model_options: ['棉-再生纤'] });
    areaApi.getStatus.mockResolvedValue({ ok: true, issues: [] });
    areaApi.listRecentFolders.mockResolvedValue({
      items: [{
        folder_name: '260144785_辜_面积法',
        updated_at: '2026-07-28T00:00:00Z',
      }],
    });
    areaApi.searchFolders.mockResolvedValue({ items: [] });
    areaApi.listFolderPreviewImages.mockResolvedValue({
      items: [{ name: 'sample_1.jpg' }],
    });
    areaApi.getFolderImageUrl.mockImplementation((folder, name) => `/preview/${folder}/${name}`);
    areaApi.createJob.mockResolvedValue({ job_id: 'job-001' });
    areaApi.previewFolderCleanup.mockResolvedValue({
      move_count: 1,
      keep_count: 6,
      rename_target_exists: false,
    });
    areaApi.cleanupFolder.mockResolvedValue({
      moved: 1,
      old_folder: '260144785_辜_面积法',
      new_folder: '260144785_辜_面积法',
      renamed: false,
    });
  });

  it('进入页面即可选择目录并开始识别，提交后直接进入任务记录', async () => {
    const user = userEvent.setup();
    renderStartPage();

    expect(screen.getByText('选择数据目录')).toBeInTheDocument();
    expect(screen.queryByText('选择数据目录，开始识别')).not.toBeInTheDocument();
    expect(screen.queryByText('先选择本次检测的数据目录，核对预览图片和识别模型后即可提交。')).not.toBeInTheDocument();
    const startButton = screen.getByRole('button', { name: /开始识别/ });
    expect(startButton).toBeDisabled();

    await user.click(await screen.findByText('260144785_辜_面积法'));

    await waitFor(() => {
      expect(areaApi.listFolderPreviewImages).toHaveBeenCalledWith(
        '260144785_辜_面积法',
        { limit: 6 },
      );
    });
    expect(screen.getByText('棉-再生纤')).toBeInTheDocument();
    expect(startButton).toBeEnabled();

    await user.click(startButton);

    await waitFor(() => {
      expect(areaApi.createJob).toHaveBeenCalledWith({
        folder_name: '260144785_辜_面积法',
        model_name: '棉-再生纤',
      });
    });
    expect(await screen.findByText('任务记录')).toBeInTheDocument();
  });

  it('快速连续搜索时不会让旧响应覆盖较新的目录结果', async () => {
    const user = userEvent.setup();
    const oldRequest = deferred();
    const newRequest = deferred();
    areaApi.searchFolders
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);
    renderStartPage();

    const input = screen.getByPlaceholderText('输入编号或文件夹名称');
    await user.type(input, 'old{enter}');
    await user.clear(input);
    await user.type(input, 'new{enter}');

    await act(async () => {
      newRequest.resolve({ items: [{ folder_name: 'new_result', updated_at: null }] });
    });
    expect(await screen.findByText('new_result')).toBeInTheDocument();

    await act(async () => {
      oldRequest.resolve({ items: [{ folder_name: 'old_result', updated_at: null }] });
    });
    expect(screen.queryByText('old_result')).not.toBeInTheDocument();
    expect(screen.getByText('new_result')).toBeInTheDocument();
  });

  it('操作列直接打开目录整理，并且不会误选该目录', async () => {
    const user = userEvent.setup();
    areaApi.previewFolderCleanup.mockReturnValue(new Promise(() => {}));
    renderStartPage();

    const cleanupButton = await screen.findByRole('button', {
      name: '整理目录：260144785_辜_面积法',
    });
    expect(screen.getByRole('button', { name: /开始识别/ })).toBeDisabled();

    await user.click(cleanupButton);

    expect(await screen.findByText('整理采集目录')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认整理' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /开始识别/ })).toBeDisabled();
    expect(areaApi.listFolderPreviewImages).not.toHaveBeenCalled();
  });

  it('整理当前已选目录后清空旧选择并重新加载目录', async () => {
    const user = userEvent.setup();
    renderStartPage();

    await user.click(await screen.findByText('260144785_辜_面积法'));
    expect(screen.getByRole('button', { name: /开始识别/ })).toBeEnabled();
    await user.click(screen.getByRole('button', {
      name: '整理目录：260144785_辜_面积法',
    }));

    const confirmButton = screen.getByRole('button', { name: '确认整理' });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    await user.click(confirmButton);

    await waitFor(() => {
      expect(areaApi.cleanupFolder).toHaveBeenCalledWith(
        '260144785_辜_面积法',
        { rename_enabled: false, new_folder_name: null },
      );
    });
    expect(screen.getByRole('button', { name: /开始识别/ })).toBeDisabled();
    expect(screen.getByText('请先从左侧选择数据目录')).toBeInTheDocument();
    expect(areaApi.listRecentFolders).toHaveBeenCalledTimes(2);
  });

  it('运行状态无法确认时禁止提交识别任务', async () => {
    const user = userEvent.setup();
    areaApi.getStatus.mockRejectedValue(new Error('运行状态加载失败'));
    renderStartPage();

    await user.click(await screen.findByText('260144785_辜_面积法'));

    expect(await screen.findByText('运行状态加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /开始识别/ })).toBeDisabled();
    expect(areaApi.createJob).not.toHaveBeenCalled();
  });
});
