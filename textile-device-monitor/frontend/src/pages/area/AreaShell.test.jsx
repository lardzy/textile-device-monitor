import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import AreaShell from './AreaShell';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

const renderShell = (initialEntry = '/tools/area') => render(
  <MemoryRouter
    initialEntries={[initialEntry]}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <Routes>
      <Route path="/tools/area" element={<AreaShell />}>
        <Route index element={<div>开始识别页面</div>} />
        <Route path="tasks" element={<div>任务记录页面</div>} />
        <Route path="folders" element={<div>数据目录页面</div>} />
      </Route>
    </Routes>
    <LocationProbe />
  </MemoryRouter>,
);

describe('AreaShell', () => {
  it('默认入口突出开始识别，并可明确进入独立任务记录页', async () => {
    const user = userEvent.setup();
    renderShell();

    expect(screen.getByText('开始识别页面')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /开始识别/ })).toBeChecked();

    await user.click(screen.getByText('任务记录'));

    expect(await screen.findByText('任务记录页面')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/tools/area/tasks');
    expect(screen.getByRole('radio', { name: /任务记录/ })).toBeChecked();
  });
});
