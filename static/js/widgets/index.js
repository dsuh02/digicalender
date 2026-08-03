/* Widget registry. Add a definition here and it appears in the palette, gets a
 * settings panel built from its schema, and can be placed on any page. */

import { AgendaWidget, DayWidget, MonthWidget, WeekWidget } from './calendar.js';
import { AccountsWidget, BillsWidget, NetWorthWidget } from './finance.js';
import { GalleryWidget } from './gallery.js';
import { CashflowWidget, CreditWidget, NetWorthChartWidget, SpendingWidget } from './insights.js';
import { DeviceGridWidget, MediaWidget, RokuRemoteWidget, ScenesWidget } from './home.js';
import { ClockWidget, LabelWidget, WeatherWidget } from './info.js';
import { GreetingWidget, PeopleWidget } from './people.js';
import { ProjectionWidget } from './projection.js';
import { NotificationsWidget, TodoWidget } from './tasks.js';

export const WIDGETS = [
  MonthWidget, WeekWidget, DayWidget, AgendaWidget,
  ClockWidget, GreetingWidget, WeatherWidget, LabelWidget, GalleryWidget, PeopleWidget,
  TodoWidget, NotificationsWidget,
  NetWorthWidget, NetWorthChartWidget, AccountsWidget, BillsWidget,
  SpendingWidget, CashflowWidget, CreditWidget, ProjectionWidget,
  DeviceGridWidget, ScenesWidget, RokuRemoteWidget, MediaWidget,
];

export const BY_TYPE = Object.fromEntries(WIDGETS.map(w => [w.type, w]));

export const CATEGORIES = [...new Set(WIDGETS.map(w => w.category))];

export function widgetDef(type) {
  return BY_TYPE[type] || null;
}
