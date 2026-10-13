import unittest
from datetime import datetime, timezone, timedelta
from aiusage.analytics import number, timestamp, iso, velocity

NOW=datetime(2026,10,12,12,tzinfo=timezone.utc)
RESET=iso(NOW+timedelta(hours=2))

def current(**extra):
    return {'status':'fresh','remaining_pct':20,'used_pct':80,'account_ref':'a',
            'resets_at':RESET,'window_minutes':300,**extra}

def points(values, step=150, **extra):
    return [{**current(**extra),'used_pct':v,'observed_at':iso(NOW-timedelta(seconds=(len(values)-i-1)*step))}
            for i,v in enumerate(values)]

class AnalyticsTests(unittest.TestCase):
    def test_numeric_validation(self):
        for value in (True,False,None,'NaN','Infinity',float('inf'),'1e30',{},[]): self.assertIsNone(number(value))
        self.assertEqual(str(number('0.0000000001')),'1E-10')
    def test_requires_timezone(self):
        self.assertIsNone(timestamp('2026-10-12T12:00:00'))
        self.assertIsNotNone(timestamp('2026-10-12T12:00:00-05:00'))
    def test_rate_forecast_and_safe_pace(self):
        r=velocity(points([70,75,80],1800),current(),NOW,max_gap_seconds=1800)
        self.assertEqual(r['pct_per_hour'],10)
        self.assertEqual(r['safe_pace_pct_per_hour'],10)
        self.assertFalse(r['exhausts_before_reset'])
        self.assertEqual(r['eta_exhaustion_at'],RESET)
    def test_runout_before_reset(self):
        r=velocity(points([60,70,80],1800),current(),NOW,max_gap_seconds=1800)
        self.assertTrue(r['exhausts_before_reset'])
        self.assertEqual(r['predicted_remaining_at_reset'],0)
    def test_too_few_or_short_samples(self):
        for p in (points([70,80]),points([70,75,80],60)):
            self.assertEqual(velocity(p,current(),NOW)['confidence'],'insufficient')
    def test_flat_rounded_trace_withholds_projection(self):
        r=velocity(points([80,80,80]),current(),NOW)
        self.assertEqual(r['pct_per_hour'],0)
        self.assertIsNone(r['exhausts_before_reset'])
        self.assertIsNone(r['predicted_remaining_at_reset'])
    def test_account_reset_duration_boundaries(self):
        for change in ({'account_ref':'b'},{'resets_at':iso(NOW+timedelta(days=1))},{'window_minutes':10080}):
            r=velocity(points([70,75,80],**change),current(),NOW)
            self.assertIsNone(r['pct_per_hour'])
        self.assertEqual(velocity(points([70,75,80]),current(account_ref=None),NOW)['confidence'],'unavailable')
    def test_recovery_does_not_become_negative_burn(self):
        r=velocity(points([80,70,75]),current(),NOW)
        self.assertIsNone(r['pct_per_hour'])
        self.assertEqual(r['points'],2)
    def test_stale_gap_breaks_segment(self):
        r=velocity(points([70,75,80],150),current(),NOW,max_gap_seconds=90)
        self.assertIsNone(r['pct_per_hour'])
        self.assertEqual(r['points'],1)
    def test_fresh_dense_trace_has_long_baseline(self):
        r=velocity(points([i for i in range(70,81)],30),current(),NOW,max_gap_seconds=90)
        self.assertEqual(r['pct_per_hour'],120)
        self.assertEqual(r['points'],11)
    def test_unusable_current_readings(self):
        for status in ('stale','unknown','expired','unlimited'):
            r=velocity(points([70,75,80]),current(status=status),NOW)
            self.assertIsNone(r['pct_per_hour']); self.assertIsNone(r['safe_pace_pct_per_hour'])
    def test_reset_deadline_jitter_allows_same_cycle(self):
        p=points([70,75,80]);p[0]['resets_at']=iso(NOW+timedelta(hours=2,seconds=1))
        self.assertEqual(velocity(p,current(),NOW)['points'],3)
        p[0]['resets_at']=iso(NOW+timedelta(hours=2,seconds=3))
        self.assertEqual(velocity(p,current(),NOW)['points'],2)
    def test_exhausted_is_an_actual_not_a_future_forecast(self):
        r=velocity(points([90,95,100]),current(remaining_pct=0,used_pct=100),NOW)
        self.assertTrue(r['is_exhausted']);self.assertIsNotNone(r['pct_per_hour'])
        self.assertIsNone(r['eta_exhaustion_at']);self.assertIsNone(r['exhausts_before_reset'])
    def test_expired_window(self):
        self.assertEqual(velocity([],current(resets_at=iso(NOW)),NOW)['confidence'],'unavailable')
    def test_missing_cycle_boundary_withholds_rates_and_forecasts(self):
        result=velocity(points([70,75,80],resets_at=None),current(resets_at=None),NOW)
        self.assertEqual(result['confidence'],'unavailable')
        self.assertIsNone(result['pct_per_hour']);self.assertIsNone(result['eta_exhaustion_at'])
    def test_future_invalid_and_missing_points_ignored(self):
        p=points([70,75,80]);p[-1]['observed_at']=iso(NOW+timedelta(minutes=5))
        p.append({**current(),'observed_at':'invalid','used_pct':'NaN'})
        self.assertEqual(velocity(p,current(),NOW)['points'],2)

if __name__=='__main__':unittest.main()
