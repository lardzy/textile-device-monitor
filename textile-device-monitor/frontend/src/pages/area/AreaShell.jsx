import { FolderOpenOutlined, SettingOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { Button, Segmented, Space } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import './area.css';

const NAV_ITEMS = [
  { value: '/tools/area', label: '任务中心', icon: <UnorderedListOutlined /> },
  { value: '/tools/area/folders', label: '数据目录', icon: <FolderOpenOutlined /> },
];

function AreaShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const isWorkspace = location.pathname.startsWith('/tools/area/jobs/');
  const selected = location.pathname.startsWith('/tools/area/settings')
    ? null
    : (location.pathname.startsWith('/tools/area/folders') ? '/tools/area/folders' : '/tools/area');

  return (
    <div className={isWorkspace ? 'area-shell area-shell--workspace' : 'area-shell'}>
      {!isWorkspace ? (
        <div className="area-section-nav">
          <Segmented
            value={selected}
            options={NAV_ITEMS}
            onChange={(value) => navigate(value)}
          />
          <Space>
            <Button
              icon={<SettingOutlined />}
              onClick={() => navigate('/tools/area/settings')}
            >
              全局设置
            </Button>
          </Space>
        </div>
      ) : null}
      <Outlet />
    </div>
  );
}

export default AreaShell;
