import { BrowserRouter as Router, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import { Layout, Menu } from 'antd';
import { MonitorOutlined, HistoryOutlined, BarChartOutlined, SettingOutlined, ToolOutlined } from '@ant-design/icons';
import DeviceMonitor from './pages/DeviceMonitor';
import HistoryQuery from './pages/HistoryQuery';
import Statistics from './pages/Statistics';
import DeviceManagement from './pages/DeviceManagement';
import ResultsTable from './pages/ResultsTable';
import ResultsImages from './pages/ResultsImages';
import EfficiencyTools from './pages/EfficiencyTools';
import DocumentConverter from './pages/DocumentConverter';
import wsClient from './websocket/client';
import { useState, useEffect } from 'react';

const { Header, Content, Sider } = Layout;

const menuItems = [
  { key: 'monitor', icon: <MonitorOutlined />, label: '设备监控', path: '/' },
  { key: 'history', icon: <HistoryOutlined />, label: '历史记录', path: '/history' },
  { key: 'statistics', icon: <BarChartOutlined />, label: '数据统计', path: '/statistics' },
  { key: 'management', icon: <SettingOutlined />, label: '设备管理', path: '/management' },
  { key: 'efficiency', icon: <ToolOutlined />, label: '效率工具', path: '/efficiency' },
];

const appRoutes = (
  <Routes>
    <Route path="/" element={<DeviceMonitor />} />
    <Route path="/history" element={<HistoryQuery />} />
    <Route path="/statistics" element={<Statistics />} />
    <Route path="/management" element={<DeviceManagement />} />
    <Route path="/efficiency" element={<EfficiencyTools />} />
    <Route path="/efficiency/document-converter" element={<DocumentConverter />} />
    <Route path="/results/table" element={<ResultsTable />} />
    <Route path="/results/images" element={<ResultsImages />} />
  </Routes>
);

function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  // 计算选中的菜单项
  const getSelectedKey = () => {
    const path = location.pathname;

    // 检查是否是效率工具相关路径
    if (path.startsWith('/efficiency')) {
      return 'efficiency';
    }

    // 检查是否是结果相关路径
    if (path.startsWith('/results')) {
      return null; // 不显示任何菜单项选中
    }

    // 精确匹配路径
    const matched = menuItems.find(item => item.path === path);
    return matched?.key || 'monitor';
  };

  const selectedKey = getSelectedKey();
  const isResults = location.pathname.startsWith('/results');

  useEffect(() => {
    const baseUrl = import.meta.env.VITE_WS_URL;
    const wsUrl = baseUrl
      || `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
    wsClient.connect(wsUrl);

    return () => {
      wsClient.disconnect();
    };
  }, []);

  const handleMenuClick = ({ key }) => {
    const target = menuItems.find(item => item.key === key);
    if (target) {
      navigate(target.path);
    }
  };

  if (isResults) {
    return (
      <Layout style={{ minHeight: '100vh' }}>
        <Content style={{ margin: '16px', padding: 16, background: '#fff', borderRadius: '8px' }}>
          {appRoutes}
        </Content>
      </Layout>
    );
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed} theme="dark">
        <div style={{ padding: '16px', color: 'white', textAlign: 'center', fontSize: '18px', fontWeight: 'bold' }}>
          {collapsed ? '检测' : '纺织品检测系统'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={selectedKey ? [selectedKey] : []}
          items={menuItems}
          onClick={handleMenuClick}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', padding: '0 24px', boxShadow: '0 2px 8px rgba(0,0,0,0.1)' }}>
          <h2 style={{ margin: 0 }}>{menuItems.find(item => item.key === selectedKey)?.label}</h2>
        </Header>
        <Content style={{ margin: '24px 16px', padding: 24, background: '#fff', borderRadius: '8px' }}>
          {appRoutes}
        </Content>
      </Layout>
    </Layout>
  );
}

function App() {
  return (
    <Router>
      <AppLayout />
    </Router>
  );
}

export default App;
