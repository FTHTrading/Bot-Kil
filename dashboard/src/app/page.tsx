'use client';

import React, { useState } from 'react';
import CommandBar from '@/components/dashboard/CommandBar';
import PerformanceStrip from '@/components/dashboard/PerformanceStrip';
import { EquityCurveCard, KellyCalcCard } from '@/components/charts/Charts';
import IntelligenceTabs from '@/components/dashboard/IntelligenceTabs';
import DetailDrawer, { DrawerPayload } from '@/components/dashboard/DetailDrawer';
import ActivityFeed from '@/components/dashboard/ActivityFeed';
import BackOffice from '@/components/dashboard/BackOffice';
import LiveAgentPanel from '@/components/dashboard/LiveAgentPanel';

export default function Dashboard() {
  const [drawerPayload, setDrawerPayload] = useState<DrawerPayload | null>(null);

  return (
    <div className="min-h-screen flex flex-col bg-[#020912]">

      {/* Ambient background orbs */}
      <div className="fixed inset-0 pointer-events-none overflow-hidden" style={{ zIndex: 0 }}>
        <div
          className="absolute -top-40 -left-40 w-96 h-96 rounded-full blur-3xl"
          style={{ background: 'radial-gradient(circle, rgba(0,232,122,0.055) 0%, transparent 65%)' }}
        />
        <div
          className="absolute top-1/3 -right-40 w-80 h-80 rounded-full blur-3xl"
          style={{ background: 'radial-gradient(circle, rgba(59,130,246,0.05) 0%, transparent 65%)' }}
        />
        <div
          className="absolute bottom-20 left-1/3 w-72 h-72 rounded-full blur-3xl"
          style={{ background: 'radial-gradient(circle, rgba(168,85,247,0.04) 0%, transparent 65%)' }}
        />
      </div>

      {/* Top command bar */}
      <CommandBar />

      {/* Main scrollable content */}
      <main
        className="relative flex-1 px-5 py-5 space-y-5 w-full mx-auto"
        style={{ zIndex: 1, maxWidth: '1600px' }}
      >
        {/* KPI performance strip */}
        <PerformanceStrip
          onMetricClick={(id) =>
            setDrawerPayload({ type: 'metric', data: { id, label: id, value: '' } })
          }
        />

        {/* Live agent panel (full width) + kelly calculator */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2">
            <LiveAgentPanel />
          </div>
          <div>
            <KellyCalcCard />
          </div>
        </div>

        {/* Charts row: equity curve (2/3) + activity feed (1/3) */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2">
            <EquityCurveCard />
          </div>
          <div>
            <ActivityFeed />
          </div>
        </div>

        {/* Intelligence workspace */}
        <IntelligenceTabs
          onPickSelect={(p) =>
            setDrawerPayload({
              type: 'pick',
              data: p as Parameters<typeof setDrawerPayload>[0] extends { type: 'pick'; data: infer D } ? D : never,
            })
          }
          onArbSelect={(a) =>
            setDrawerPayload({
              type: 'arb',
              data: a as Parameters<typeof setDrawerPayload>[0] extends { type: 'arb'; data: infer D } ? D : never,
            })
          }
          onBetSelect={(b) =>
            setDrawerPayload({
              type: 'bet',
              data: b as Parameters<typeof setDrawerPayload>[0] extends { type: 'bet'; data: infer D } ? D : never,
            })
          }
        />

        {/* Back-office: workflows + API health */}
        <BackOffice />
      </main>

      {/* Slide-in detail drawer */}
      <DetailDrawer
        payload={drawerPayload}
        onClose={() => setDrawerPayload(null)}
      />
    </div>
  );
}
