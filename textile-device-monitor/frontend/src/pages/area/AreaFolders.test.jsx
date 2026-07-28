import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { areaApi } from '../../api/area';
import AreaFolders from './AreaFolders';

vi.mock('../../api/area', () => ({
  areaApi: {
    listRecentFolders: vi.fn(),
    searchFolders: vi.fn(),
    listFolderImages: vi.fn(),
    getFolderImageUrl: vi.fn(),
    previewFolderCleanup: vi.fn(),
    cleanupFolder: vi.fn(),
  },
}));

describe('AreaFolders', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    areaApi.listRecentFolders.mockResolvedValue({
      items: [{
        folder_name: '260144785_辜_面积法',
        updated_at: '2026-07-28T00:00:00Z',
      }],
      total: 1,
      page: 1,
    });
    areaApi.previewFolderCleanup.mockResolvedValue({
      move_count: 0,
      keep_count: 1,
      rename_target_exists: false,
    });
  });

  it('整理目录时默认使用首个下划线前的目录名称', async () => {
    const user = userEvent.setup();
    render(<AreaFolders />);

    await screen.findByText('260144785_辜_面积法');
    await user.click(screen.getByRole('button', { name: '整理目录：260144785_辜_面积法' }));
    await user.click(screen.getByRole('checkbox', { name: '整理后重命名文件夹' }));

    expect(screen.getByPlaceholderText('新的文件夹名称')).toHaveValue('260144785');
  });
});
