from collections import defaultdict, namedtuple
from math import e

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Count, Max, Q, ExpressionWrapper, Case, When
from django.db.models.fields import FloatField
from django.utils import timezone
from django.utils.translation import ugettext as _, gettext_noop

from judge import event_poster as event
from judge.models import Submission, Problem

__all__ = ['contest_completed_ids', 'get_result_data', 'add_to_result_data',
           'user_completed_ids', 'user_authored_ids', 'user_editable_ids']


def user_authored_ids(profile):
    result = set(Problem.objects.filter(authors=profile).values_list('id', flat=True))
    return result


def user_editable_ids(profile):
    result = set((Problem.objects.filter(authors=profile) |
                  Problem.objects.filter(curators=profile)).values_list('id', flat=True))
    return result


def contest_completed_ids(participation):
    key = 'contest_complete:%d' % participation.id
    result = cache.get(key)
    if result is None:
        result = set(participation.submissions.filter(submission__result='AC', points=F('problem__points'))
                     .values_list('problem__problem__id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def user_completed_ids(profile):
    key = 'user_complete:%d' % profile.id
    result = cache.get(key)
    if result is None:
        result = set(Submission.objects.filter(user=profile, result='AC', points=F('problem__points'))
                     .values_list('problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def contest_attempted_ids(participation):
    key = 'contest_attempted:%s' % participation.id
    result = cache.get(key)
    if result is None:
        result = {id: {'achieved_points': points, 'max_points': max_points}
                  for id, max_points, points in (participation.submissions
                                                 .values_list('problem__problem__id', 'problem__points')
                                                 .annotate(points=Max('points'))
                                                 .filter(points__lt=F('problem__points')))}
        cache.set(key, result, 86400)
    return result


def user_attempted_ids(profile):
    key = 'user_attempted:%s' % profile.id
    result = cache.get(key)
    if result is None:
        result = {id: {'achieved_points': points, 'max_points': max_points}
                  for id, max_points, points in (Submission.objects.filter(user=profile)
                                                 .values_list('problem__id', 'problem__points')
                                                 .annotate(points=Max('points'))
                                                 .filter(points__lt=F('problem__points')))}
        cache.set(key, result, 86400)
    return result


ResultType = namedtuple('ResultType', 'code name results')

# Using gettext_noop here since this will be tacked into the cache, so it must be language neutral.
# The ultimate consumer, SubmissionList.get_result_data will run ugettext on the name.
RESULT_TYPES = [
    ResultType('AC', gettext_noop('Accepted'), {'AC'}),
    ResultType('WA', gettext_noop('Wrong'), {'WA'}),
    ResultType('CE', gettext_noop('Compile Error'), {'CE'}),
    ResultType('TLE', gettext_noop('Timeout'), {'TLE'}),
    ResultType('ERR', gettext_noop('Error'), {'MLE', 'OLE', 'IR', 'RTE', 'AB', 'IE'}),
]

RESULT_TO_TYPE = {status: rtype.code for rtype in RESULT_TYPES for status in rtype.results}


def get_result_data(*args, **kwargs):
    if args:
        submissions = args[0]
        if kwargs:
            raise ValueError(_("Can't pass both queryset and keyword filters"))
    else:
        submissions = Submission.objects.filter(**kwargs) if kwargs is not None else Submission.objects
    raw = submissions.values('result').annotate(count=Count('result')).values_list('result', 'count')
    results = defaultdict(int, raw)

    return {
        'categories': [
            {'code': rtype.code, 'name': rtype.name, 'count': sum(results[status] for status in rtype.results)}
            for rtype in RESULT_TYPES
        ],
        'total': sum(results.values()),
    }


def add_to_result_data(result, delta):
    if result not in RESULT_TO_TYPE:
        return

    cache_key = 'global_submission_result_data'
    result_data = cache.get(cache_key)
    if not result_data:
        return

    code = RESULT_TO_TYPE.get(result)
    for category in result_data['categories']:
        if category['code'] == code:
            category['count'] += delta
            result_data['total'] += delta
            break

    cache.set(cache_key, result_data, settings.GLOBAL_SUBMISSION_STAT_UPDATE_INTERVAL)

    event.post('submissions', {
        'type': 'change-global-stats',
        'result_type': RESULT_TO_TYPE[result],
        'delta': delta
    })


def editable_problems(user, profile=None):
    subquery = Problem.objects.all()
    if profile is None:
        profile = user.profile
    if not user.has_perm('judge.edit_all_problem'):
        subfilter = Q(authors__id=profile.id) | Q(curators__id=profile.id)
        if user.has_perm('judge.edit_public_problem'):
            subfilter |= Q(is_public=True)
        subquery = subquery.filter(subfilter)
    return subquery


def hot_problems(duration, limit):
    cache_key = 'hot_problems:%d:%d' % (duration.total_seconds(), limit)
    qs = cache.get(cache_key)
    if qs is None:
        qs = Problem.objects.filter(is_public=True, is_organization_private=False, submission__date__gt=timezone.now() - duration, points__gt=3, points__lt=25)
        qs0 = qs.annotate(k=Count('submission__user', distinct=True)).order_by('-k').values_list('k', flat=True)

        if not qs0:
            return []
        # make this an aggregate
        mx = float(qs0[0])

        qs = qs.annotate(unique_user_count=Count('submission__user', distinct=True))
        # fix braindamage in excluding CE
        qs = qs.annotate(submission_volume=Count(Case(
                When(submission__result='AC', then=1),
                When(submission__result='WA', then=1),
                When(submission__result='IR', then=1),
                When(submission__result='RTE', then=1),
                When(submission__result='TLE', then=1),
                When(submission__result='OLE', then=1),
                output_field=FloatField(),
            )))
        qs = qs.annotate(ac_volume=Count(Case(
                When(submission__result='AC', then=1),
                output_field=FloatField(),
            )))
        qs = qs.filter(unique_user_count__gt=max(mx / 3.0, 1))

        qs = qs.annotate(ordering=ExpressionWrapper(0.5 * F('points') * (0.4 * F('ac_volume') / F('submission_volume') + 0.6 * F('ac_rate')) + 100 * e ** (F('unique_user_count') / mx), output_field=FloatField())).order_by('-ordering').defer('description')[:limit]

        cache.set(cache_key, qs, 900)
    return qs
