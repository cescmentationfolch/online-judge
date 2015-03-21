from operator import itemgetter, attrgetter
from django.conf import settings

from django.contrib import admin, messages
from django.conf.urls import patterns, url
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.core.cache import cache
from django.db.models import TextField, Q
from django.forms import ModelForm, ModelMultipleChoiceField, TextInput
from django.http import HttpResponseRedirect, Http404
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from mptt.admin import MPTTModelAdmin
import reversion

from judge.dblock import LockModel
from judge.models import Language, Profile, Problem, ProblemGroup, ProblemType, Submission, Comment, \
    MiscConfig, Judge, NavigationBar, Contest, ContestParticipation, ContestProblem, Organization, BlogPost, \
    ContestProfile, SubmissionTestCase, Solution
from judge.ratings import rate_contest
from judge.widgets import CheckboxSelectMultipleWithSelectAll, AdminPagedownWidget, MathJaxAdminPagedownWidget

try:
    from django_select2.widgets import HeavySelect2Widget, HeavySelect2MultipleWidget, Select2Widget, Select2MultipleWidget
except ImportError:
    HeavySelect2Widget = None
    HeavySelect2MultipleWidget = None
    Select2Widget = None
    Select2MultipleWidget = None

try:
    from suit.admin import SortableModelAdmin
except ImportError:
    SortableModelAdmin = object

use_select2 = HeavySelect2MultipleWidget is not None and 'django_select2' in settings.INSTALLED_APPS


class Select2SuitMixin(object):
    if 'suit' in settings.INSTALLED_APPS and use_select2:
        class Media:
            css = {
                'all': ('admin/css/select2bootstrap.css',)
            }


class ContestProfileInlineForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ContestProfileInlineForm, self).__init__(*args, **kwargs)
        self.fields['current'].queryset = self.instance.history.select_related('contest').select_related('contest')
        self.fields['current'].label_from_instance = lambda obj: obj.contest.name

    class Meta:
        if use_select2:
            widgets = {
                'current': Select2Widget,
            }


class ContestProfileInline(admin.StackedInline):
    fields = ('current',)
    model = ContestProfile
    form = ContestProfileInlineForm
    can_delete = False


class ProfileForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'timezone': Select2Widget,
                'language': Select2Widget,
                'ace_theme': Select2Widget,
                'organization': HeavySelect2Widget(data_view='organization_select2'),
            }


class TimezoneFilter(admin.SimpleListFilter):
    title = 'Location'
    parameter_name = 'timezone'

    def lookups(self, request, model_admin):
        return Profile.objects.values_list('timezone', 'timezone').distinct().order_by('timezone')

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        return queryset.filter(timezone=self.value())


class ProfileAdmin(Select2SuitMixin, reversion.VersionAdmin):
    fields = ('user', 'name', 'display_rank', 'about', 'organization', 'timezone', 'language', 'ace_theme',
              'last_access', 'ip', 'mute')
    readonly_fields = ('user',)
    list_display = ('admin_user_admin', 'email', 'timezone_full', 'language', 'last_access', 'ip')
    ordering = ('user__username',)
    search_fields = ('user__username', 'name', 'ip', 'user__email')
    list_filter = ('language', TimezoneFilter)
    actions = ('recalculate_points',)
    inlines = [ContestProfileInline]
    actions_on_top = True
    actions_on_bottom = True
    form = ProfileForm

    def admin_user_admin(self, obj):
        return obj.long_display_name
    admin_user_admin.admin_order_field = 'user__username'
    admin_user_admin.short_description = 'User'

    def email(self, obj):
        return obj.user.email
    email.admin_order_field = 'user__email'
    email.short_description = 'Email'

    def timezone_full(self, obj):
        return obj.timezone

    timezone_full.admin_order_field = 'timezone'
    timezone_full.short_description = 'Timezone'

    def recalculate_points(self, request, queryset):
        count = 0
        for profile in queryset:
            profile.calculate_points()
            count += 1
        self.message_user(request, "%d user%s have scores recalculated." % (count, 's'[count == 1:]))

    recalculate_points.short_description = 'Recalculate scores'


class ProblemForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ProblemForm, self).__init__(*args, **kwargs)
        self.fields['authors'].widget.can_add_related = False
        self.fields['banned_users'].widget.can_add_related = False

    class Meta:
        if use_select2:
            widgets = {
                'authors': HeavySelect2MultipleWidget(data_view='profile_select2'),
                'banned_users': HeavySelect2MultipleWidget(data_view='profile_select2'),
                'types': Select2MultipleWidget,
                'group': Select2Widget,
            }


class ProblemCreatorListFilter(admin.SimpleListFilter):
    title = parameter_name = 'creator'

    def lookups(self, request, model_admin):
        return [(name, '%s (%s)' % (name, display) if display else name)for name, display in
                Profile.objects.exclude(authored_problems=None).values_list('user__username', 'name')]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        return queryset.filter(authors__user__username=self.value())


class ProblemAdmin(Select2SuitMixin, reversion.VersionAdmin):
    fieldsets = (
        (None, {
            'fields': ('code', 'name', 'is_public', 'date', 'authors', 'description')
        }),
        ('Taxonomy', {'fields': ('types', 'group')}),
        ('Points', {'fields': (('points', 'partial'), 'short_circuit')}),
        ('Limits', {'fields': ('time_limit', 'memory_limit')}),
        ('Language', {'fields': ('allowed_languages',)}),
        ('Justice', {'fields': ('banned_users',)})
    )
    list_display = ['code', 'name', 'show_authors', 'points', 'is_public']
    ordering = ['code']
    search_fields = ('code', 'name')
    actions = ['make_public', 'make_private']
    list_per_page = 500
    list_max_show_all = 1000
    actions_on_top = True
    actions_on_bottom = True
    list_filter = ('is_public', ProblemCreatorListFilter)
    form = ProblemForm

    if not use_select2:
        filter_horizontal = ['authors', 'banned_users']

    if MathJaxAdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': MathJaxAdminPagedownWidget},
        }

    def show_authors(self, obj):
        return ', '.join(map(attrgetter('user.username'), obj.authors.select_related('user')))
    show_authors.short_description = 'Authors'

    def make_public(self, request, queryset):
        count = queryset.update(is_public=True)
        self.message_user(request, "%d problem%s successfully marked as public." % (count, 's'[count == 1:]))

    make_public.short_description = 'Mark problems as public'

    def make_private(self, request, queryset):
        count = queryset.update(is_public=False)
        self.message_user(request, "%d problem%s successfully marked as private." % (count, 's'[count == 1:]))

    make_private.short_description = 'Mark problems as private'

    def get_queryset(self, request):
        if request.user.has_perm('judge.edit_all_problem'):
            return Problem.objects.all()
        else:
            return Problem.objects.filter(authors__id=request.user.profile.id)

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_problem'):
            return False
        if request.user.has_perm('judge.edit_all_problem') or obj is None:
            return True
        return obj.authors.filter(id=request.user.profile.id).exists()

    def formfield_for_manytomany(self, db_field, request=None, **kwargs):
        if db_field.name == 'allowed_languages':
            kwargs['widget'] = CheckboxSelectMultipleWithSelectAll()
        return super(ProblemAdmin, self).formfield_for_manytomany(db_field, request, **kwargs)

    def get_form(self, *args, **kwargs):
        form = super(ProblemAdmin, self).get_form(*args, **kwargs)
        form.base_fields['authors'].queryset = Profile.objects.all()
        return form


class SubmissionStatusFilter(admin.SimpleListFilter):
    parameter_name = title = 'status'
    __lookups = (('None', 'None'), ('NotDone', 'Not done'), ('EX', 'Exceptional')) + Submission.STATUS
    __handles = set(map(itemgetter(0), Submission.STATUS))

    def lookups(self, request, model_admin):
        return self.__lookups

    def queryset(self, request, queryset):
        if self.value() == 'None':
            return queryset.filter(status=None)
        elif self.value() == 'NotDone':
            return queryset.exclude(status__in=['D', 'IE', 'CE', 'AB'])
        elif self.value() == 'EX':
            return queryset.exclude(status__in=['D', 'CE', 'G', 'AB'])
        elif self.value() in self.__handles:
            return queryset.filter(status=self.value())


class SubmissionResultFilter(admin.SimpleListFilter):
    parameter_name = title = 'result'
    __lookups = (('None', 'None'), ('BAD', 'Unaccepted')) + Submission.RESULT
    __handles = set(map(itemgetter(0), Submission.RESULT))

    def lookups(self, request, model_admin):
        return self.__lookups

    def queryset(self, request, queryset):
        if self.value() == 'None':
            return queryset.filter(result=None)
        elif self.value() == 'BAD':
            return queryset.exclude(result='AC')
        elif self.value() in self.__handles:
            return queryset.filter(result=self.value())


class SubmissionTestCaseInline(admin.TabularInline):
    fields = ('case', 'batch', 'status', 'time', 'memory', 'points', 'total')
    readonly_fields = ('case', 'batch', 'total')
    model = SubmissionTestCase
    can_delete = False
    max_num = 0


class SubmissionAdmin(admin.ModelAdmin):
    readonly_fields = ('user', 'problem', 'date')
    fields = ('user', 'problem', 'date', 'time', 'memory', 'points', 'language', 'source', 'status', 'result',
              'case_points', 'case_total')
    actions = ('judge', 'recalculate_score')
    list_display = ('id', 'problem_code', 'problem_name', 'user_column', 'execution_time', 'pretty_memory',
                    'points', 'language', 'status', 'result', 'judge_column')
    list_filter = ('language', SubmissionStatusFilter, SubmissionResultFilter)
    search_fields = ('problem__code', 'problem__name', 'user__user__username', 'user__name')
    actions_on_top = True
    actions_on_bottom = True
    inlines = [SubmissionTestCaseInline]

    def user_column(self, obj):
        return format_html(u'<span title="{display}">{username}</span>',
                           username=obj.user.user.username,
                           display=obj.user.name)
    user_column.admin_order_field = 'user__user__username'
    user_column.short_description = 'User'

    def get_queryset(self, request):
        if request.user.has_perm('judge.edit_all_problem'):
            return Submission.objects.all()
        else:
            return Submission.objects.filter(problem__authors__id=request.user.profile.id)

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_problem'):
            return False
        if request.user.has_perm('judge.edit_all_problem') or obj is None:
            return True
        return obj.problem.authors.filter(id=request.user.profile.id).exists()

    def judge(self, request, queryset):
        if not request.user.has_perm('judge.rejudge_submission') or not request.user.has_perm('judge.edit_own_problem'):
            self.message_user(request, 'You do not have the permission to rejudge submissions.', level=messages.ERROR)
            return
        successful = 0
        queryset = queryset.order_by('id')
        if queryset.count() > 10 and not request.user.has_perm('judge.rejudge_submission_lot'):
            self.message_user(request, 'You do not have the permission to rejudge THAT many submissions.',
                              level=messages.ERROR)
            return
        if not request.user.has_perm('judge.edit_all_problem'):
            queryset = queryset.filter(problem__authors__id=request.user.profile.id)
        for model in queryset:
            successful += model.judge()
        self.message_user(request, '%d submission%s were successfully scheduled for rejudging.' %
                          (successful, 's'[successful == 1:]))
    judge.short_description = 'Rejudge the selected submissions'

    def execution_time(self, obj):
        return round(obj.time, 2) if obj.time is not None else 'None'
    execution_time.admin_order_field = 'time'

    def pretty_memory(self, obj):
        memory = obj.memory
        if memory is None:
            return 'None'
        if memory < 1000:
            return '%d KB' % memory
        else:
            return '%.2f MB' % (memory / 1024.)
    pretty_memory.admin_order_field = 'memory'
    pretty_memory.short_description = 'Memory Usage'

    def recalculate_score(self, request, queryset):
        if not request.user.has_perm('judge.rejudge_submission'):
            self.message_user(request, 'You do not have the permission to rejudge submissions.', level=messages.ERROR)
            return
        submissions = list(queryset.select_related('problem').only('points', 'case_points', 'case_total',
                                                                   'problem__partial', 'problem__points'))
        for submission in submissions:
            submission.points = round(submission.case_points / submission.case_total * submission.problem.points
                                      if submission.case_total else 0, 1)
            if not submission.problem.partial and submission.points < submission.problem.points:
                submission.points = 0
            submission.save()

        for profile in Profile.objects.filter(id__in=queryset.values_list('user_id', flat=True).distinct()):
            profile.calculate_points()
            cache.delete('user_complete:%d' % profile.id)

        self.message_user(request, '%d submission%s were successfully rescored.' %
                          (len(submissions), 's'[len(submissions) == 1:]))
    recalculate_score.short_description = 'Rescore the selected submissions'

    def problem_code(self, obj):
        return obj.problem.code
    problem_code.admin_order_field = 'problem__code'

    def problem_name(self, obj):
        return obj.problem.name
    problem_name.admin_order_field = 'problem__name'

    def get_urls(self):
        urls = super(SubmissionAdmin, self).get_urls()
        my_urls = patterns('',
                           url(r'^(\d+)/judge/$', self.judge_view, name='judge_submission_rejudge'),
        )
        return my_urls + urls

    def judge_view(self, request, id):
        if not request.user.has_perm('judge.rejudge_submission') or not request.user.has_perm('judge.edit_own_problem'):
            raise PermissionDenied()
        try:
            submission = Submission.objects.get(id=id)
        except ObjectDoesNotExist:
            raise Http404()
        if not request.user.has_perm('judge.edit_all_problem') and \
                not submission.problem.authors.filter(id=request.user.profile.id).exists():
            raise PermissionDenied()
        submission.judge()
        return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/'))

    def judge_column(self, obj):
        return '<input type="button" value="Rejudge" onclick="location.href=\'%s/judge/\'" />' % obj.id

    judge_column.short_description = ''
    judge_column.allow_tags = True


class CommentForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'author': HeavySelect2Widget(data_view='profile_select2'),
                'parent': HeavySelect2Widget(data_view='comment_select2'),
            }


class CommentAdmin(Select2SuitMixin, reversion.VersionAdmin):
    fieldsets = (
        (None, {'fields': ('author', 'page', 'parent', 'score', 'hidden')}),
        ('Content', {'fields': ('title', 'body')}),
    )
    list_display = ['title', 'author', 'linked_page', 'time']
    search_fields = ['author__user__username', 'author__name', 'page', 'title', 'body']
    actions = ['hide_comment', 'unhide_comment']
    list_filter = ['hidden']
    actions_on_top = True
    actions_on_bottom = True
    form = CommentForm

    def hide_comment(self, request, queryset):
        count = queryset.update(hidden=True)
        self.message_user(request, "%d comment%s successfully hidden." % (count, 's'[count == 1:]))
    hide_comment.short_description = 'Hide comments'

    def unhide_comment(self, request, queryset):
        count = queryset.update(hidden=False)
        self.message_user(request, "%d comment%s successfully unhidden." % (count, 's'[count == 1:]))
    unhide_comment.short_description = 'Unhide comments'

    def get_queryset(self, request):
        return Comment.objects.order_by('-time')

    def linked_page(self, obj):
        link = obj.link

        if link is not None:
            return format_html('<a href="{0}">{1}</a>', link, obj.page)
        else:
            return format_html('{0}', obj.page)
    linked_page.short_description = 'Associated page'
    linked_page.allow_tags = True
    linked_page.admin_order_field = 'page'

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': AdminPagedownWidget},
        }


class LanguageForm(ModelForm):
    problems = ModelMultipleChoiceField(
        label='Disallowed problems',
        queryset=Problem.objects.all(),
        required=False,
        help_text='These problems are NOT allowed to be submitted in this language',
        widget=HeavySelect2MultipleWidget(data_view='problem_select2') if use_select2 else
               FilteredSelectMultiple('problems', False))


class LanguageAdmin(Select2SuitMixin, reversion.VersionAdmin):
    fields = ('key', 'name', 'short_name', 'common_name', 'ace', 'pygments', 'info', 'description', 'problems')
    list_display = ('key', 'name', 'common_name', 'info')
    form = LanguageForm

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': AdminPagedownWidget},
        }

    def save_model(self, request, obj, form, change):
        super(LanguageAdmin, self).save_model(request, obj, form, change)
        obj.problem_set = Problem.objects.exclude(id__in=form.cleaned_data['problems'].values('id'))

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['problems'].initial = \
            Problem.objects.exclude(id__in=obj.problem_set.values('id')).values_list('pk', flat=True) if obj else []
        return super(LanguageAdmin, self).get_form(request, obj, **kwargs)


class ProblemGroupForm(ModelForm):
    problems = ModelMultipleChoiceField(
        label='Included problems',
        queryset=Problem.objects.all(),
        required=False,
        help_text='These problems are included in this group of problems',
        widget=HeavySelect2MultipleWidget(data_view='problem_select2') if use_select2 else
               FilteredSelectMultiple('problems', False))


class ProblemGroupAdmin(Select2SuitMixin, admin.ModelAdmin):
    fields = ('name', 'full_name', 'problems')
    form = ProblemGroupForm

    def save_model(self, request, obj, form, change):
        super(ProblemGroupAdmin, self).save_model(request, obj, form, change)
        obj.problem_set = form.cleaned_data['problems']
        obj.save()

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['problems'].initial = [o.pk for o in obj.problem_set.all()] if obj else []
        return super(ProblemGroupAdmin, self).get_form(request, obj, **kwargs)


class ProblemTypeForm(ModelForm):
    problems = ModelMultipleChoiceField(
        label='Included problems',
        queryset=Problem.objects.all(),
        required=False,
        help_text='These problems are included in this type of problems',
        widget=HeavySelect2MultipleWidget(data_view='problem_select2') if use_select2 else
               FilteredSelectMultiple('problems', False))


class ProblemTypeAdmin(Select2SuitMixin, admin.ModelAdmin):
    fields = ('name', 'full_name', 'problems')
    form = ProblemTypeForm

    def save_model(self, request, obj, form, change):
        super(ProblemTypeAdmin, self).save_model(request, obj, form, change)
        obj.problem_set.clear()
        for problem in form.cleaned_data['problems']:
            obj.problem_set.add(problem)

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['problems'].initial = [o.pk for o in obj.problem_set.all()] if obj else []
        return super(ProblemTypeAdmin, self).get_form(request, obj, **kwargs)


class NavigationBarAdmin(MPTTModelAdmin, SortableModelAdmin):
    list_display = ('label', 'key', 'path')
    fields = ('key', 'label', 'path', 'regex', 'parent')
    list_editable = ()  # Bug in SortableModelAdmin: 500 without list_editable being set
    mptt_level_indent = 20
    sortable = 'order'

    def __init__(self, *args, **kwargs):
        super(NavigationBarAdmin, self).__init__(*args, **kwargs)
        self.__save_model_calls = 0

    def save_model(self, request, obj, form, change):
        self.__save_model_calls += 1
        return super(NavigationBarAdmin, self).save_model(request, obj, form, change)

    def changelist_view(self, request, extra_context=None):
        self.__save_model_calls = 0
        with NavigationBar.objects.disable_mptt_updates():
            result = super(NavigationBarAdmin, self).changelist_view(request, extra_context)
        if self.__save_model_calls:
            with LockModel(write=(NavigationBar,)):
                NavigationBar.objects.rebuild()
        return result


class GenerateKeyTextInput(TextInput):
    def render(self, name, value, attrs=None):
        text = super(TextInput, self).render(name, value, attrs)
        return mark_safe(text + format_html(
            '''\
<a href="#" onclick="return false;" class="button" id="id_{0}_regen">Regenerate</a>
<script type="text/javascript">
(function ($) {{
    $(document).ready(function () {{
        $('#id_{0}_regen').click(function () {{
            var length = 100,
                charset = "abcdefghijklnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789`~!@#$%^&*()_+-=|[]{{}};:,<>./?",
                key = "";
            for (var i = 0, n = charset.length; i < length; ++i) {{
                key += charset.charAt(Math.floor(Math.random() * n));
            }}
            $('#id_{0}').val(key);
        }});
    }});
}})(django.jQuery);
</script>
''', name))


class JudgeAdminForm(ModelForm):
    class Meta:
        widgets = {
            'auth_key': GenerateKeyTextInput(),
        }


class JudgeAdmin(reversion.VersionAdmin):
    form = JudgeAdminForm
    readonly_fields = ('created', 'online', 'last_connect', 'ping', 'load', 'runtimes', 'problems')
    fieldsets = (
        (None, {'fields': ('name', 'auth_key')}),
        ('Description', {'fields': ('description',)}),
        ('Information', {'fields': ('created', 'online', 'last_connect', 'ping', 'load')}),
        ('Capabilities', {'fields': ('runtimes', 'problems')}),
    )
    list_display = ('name', 'online', 'last_connect', 'ping', 'load')
    ordering = ['name']

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.online:
            return self.readonly_fields + ('name',)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        result = super(JudgeAdmin, self).has_delete_permission(request, obj)
        if result and obj is not None:
            return not obj.online
        return result

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': AdminPagedownWidget},
        }


class ContestProblemInlineForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'problem': HeavySelect2Widget(data_view='problem_select2'),
            }


class ContestProblemInline(admin.TabularInline):
    model = ContestProblem
    verbose_name = 'Problem'
    verbose_name_plural = 'Problems'
    fields = ('problem', 'points', 'partial')
    form = ContestProblemInlineForm


class ContestForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'organizers': HeavySelect2MultipleWidget(data_view='profile_select2'),
            }


class ContestAdmin(Select2SuitMixin, reversion.VersionAdmin):
    fieldsets = (
        (None, {'fields': ('key', 'name', 'organizers', 'is_public', 'is_rated')}),
        ('Scheduling', {'fields': ('start_time', 'end_time', 'time_limit')}),
        ('Details', {'fields': ('description', 'is_external')}),
    )
    list_display = ('key', 'name', 'is_public', 'is_external', 'is_rated', 'start_time', 'end_time', 'time_limit')
    actions = ['make_public', 'make_private']
    inlines = [ContestProblemInline]
    actions_on_top = True
    actions_on_bottom = True
    form = ContestForm

    if not use_select2:
        filter_horizontal = ['organizers']

    if MathJaxAdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': MathJaxAdminPagedownWidget},
        }

    def make_public(self, request, queryset):
        count = queryset.update(is_public=True)
        self.message_user(request, "%d contest%s successfully marked as public." % (count, 's'[count == 1:]))
    make_public.short_description = 'Mark contests as public'

    def make_private(self, request, queryset):
        count = queryset.update(is_public=False)
        self.message_user(request, "%d contest%s successfully marked as private." % (count, 's'[count == 1:]))
    make_private.short_description = 'Mark contests as private'

    def get_queryset(self, request):
        if request.user.has_perm('judge.edit_all_contest'):
            return Contest.objects.all()
        else:
            return Contest.objects.filter(organizers__id=request.user.profile.id)

    def get_readonly_fields(self, request, obj=None):
        if request.user.has_perm('judge.contest_set_rated'):
            return []
        return ['is_rated']

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_contest'):
            return False
        if request.user.has_perm('judge.edit_all_contest') or obj is None:
            return True
        return obj.organizers.filter(id=request.user.profile.id).exists()

    def get_urls(self):
        urls = super(ContestAdmin, self).get_urls()
        my_urls = patterns('',
                           url(r'^(\d+)/rate/$', self.rate_view, name='judge_contest_rate'),
        )
        return my_urls + urls

    def rate_view(self, request, id):
        if not request.user.has_perm('judge.edit_all_contest') or not request.user.has_perm('judge.edit_own_contest'):
            raise PermissionDenied()

        try:
            contest = Contest.objects.get(id=id)
        except ObjectDoesNotExist:
            raise Http404()

        if not contest.is_rated:
            raise PermissionDenied()

        if not request.user.has_perm('judge.edit_all_contest') and \
                not contest.organizers.filter(id=request.user.profile.id).exists().exists():
            raise PermissionDenied()

        rate_contest(contest)
        return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/'))

    def get_form(self, *args, **kwargs):
        form = super(ContestAdmin, self).get_form(*args, **kwargs)
        perms = ('edit_own_contest', 'edit_all_contest')
        form.base_fields['organizers'].queryset = Profile.objects.filter(
            Q(user__is_superuser=True) |
            Q(user__groups__permissions__codename__in=perms) |
            Q(user__user_permissions__codename__in=perms)
        ).distinct()
        return form


class ContestParticipationForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'contest': Select2Widget(),
                'profile': HeavySelect2Widget(data_view='contest_profile_select2'),
            }


class ContestParticipationAdmin(admin.ModelAdmin):
    fields = ('contest', 'profile', 'real_start')
    list_display = ('contest', 'username', 'real_start', 'score', 'cumtime')
    actions = ['recalculate_points', 'recalculate_cumtime']
    actions_on_bottom = actions_on_top = True
    search_fields = ('contest__key', 'contest__name', 'profile__user__user__username', 'profile__user__name')
    form = ContestParticipationForm

    def username(self, obj):
        return obj.profile.user.long_display_name
    username.admin_order_field = 'profile__user__user__username'

    def recalculate_points(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.recalculate_score()
            count += 1
        self.message_user(request, "%d participation%s have scores recalculated." % (count, 's'[count == 1:]))
    recalculate_points.short_description = 'Recalculate scores'

    def recalculate_cumtime(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.update_cumtime()
            count += 1
        self.message_user(request, "%d participation%s have times recalculated." % (count, 's'[count == 1:]))
    recalculate_cumtime.short_description = 'Recalculate cumulative time'


class OrganizationForm(ModelForm):
    class Meta:
        if use_select2:
            widgets = {
                'admins': HeavySelect2MultipleWidget(data_view='profile_select2'),
                'registrant': HeavySelect2Widget(data_view='profile_select2'),
            }


class OrganizationAdmin(Select2SuitMixin, reversion.VersionAdmin):
    readonly_fields = ('creation_date',)
    fields = ('name', 'key', 'short_name', 'about', 'registrant', 'creation_date', 'admins')
    list_display = ('name', 'key', 'short_name', 'registrant', 'creation_date')
    actions_on_top = True
    actions_on_bottom = True
    form = OrganizationForm

    if not use_select2:
        filter_horizontal = ('admins',)

    if MathJaxAdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': MathJaxAdminPagedownWidget},
        }


class BlogPostAdmin(reversion.VersionAdmin):
    fieldsets = (
        (None, {'fields': ('title', 'slug', 'visible', 'sticky', 'publish_on')}),
        ('Content', {'fields': ('content',)}),
        ('Summary', {'classes': ('collapse',), 'fields': ('summary',)}),
    )
    prepopulated_fields = {'slug': ('title',)}
    list_display = ('id', 'title', 'visible', 'sticky', 'publish_on')
    list_display_links = ('id', 'title')
    ordering = ('-publish_on',)

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': AdminPagedownWidget},
        }


class SolutionAdmin(reversion.VersionAdmin):
    fields = ('url', 'title', 'is_public', 'publish_on', 'content')
    list_display = ('title', 'url')
    search_fields = ('url', 'title')

    def get_queryset(self, request):
        return Solution.objects.all()

    if MathJaxAdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': MathJaxAdminPagedownWidget},
        }


admin.site.register(Language, LanguageAdmin)
admin.site.register(Comment, CommentAdmin)
admin.site.register(Profile, ProfileAdmin)
admin.site.register(Problem, ProblemAdmin)
admin.site.register(ProblemGroup, ProblemGroupAdmin)
admin.site.register(ProblemType, ProblemGroupAdmin)
admin.site.register(Submission, SubmissionAdmin)
admin.site.register(MiscConfig)
admin.site.register(NavigationBar, NavigationBarAdmin)
admin.site.register(Judge, JudgeAdmin)
admin.site.register(Contest, ContestAdmin)
admin.site.register(ContestParticipation, ContestParticipationAdmin)
admin.site.register(Organization, OrganizationAdmin)
admin.site.register(BlogPost, BlogPostAdmin)
admin.site.register(Solution, SolutionAdmin)
