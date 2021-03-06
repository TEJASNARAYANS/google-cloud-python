# Copyright 2017, Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import pkg_resources
import os

import grpc

from google.api_core import grpc_helpers

from google.cloud.pubsub_v1 import _gapic
from google.cloud.pubsub_v1 import types
from google.cloud.pubsub_v1.gapic import subscriber_client
from google.cloud.pubsub_v1.subscriber import futures
from google.cloud.pubsub_v1.subscriber._protocol import streaming_pull_manager
from google.cloud.pubsub_v1.subscriber.policy import thread


__version__ = pkg_resources.get_distribution('google-cloud-pubsub').version


@_gapic.add_methods(subscriber_client.SubscriberClient,
                    blacklist=('pull', 'streaming_pull'))
class Client(object):
    """A subscriber client for Google Cloud Pub/Sub.

    This creates an object that is capable of subscribing to messages.
    Generally, you can instantiate this client with no arguments, and you
    get sensible defaults.

    Args:
        policy_class (class): A class that describes how to handle
            subscriptions. You may subclass the
            :class:`.pubsub_v1.subscriber.policy.base.BasePolicy`
            class in order to define your own consumer. This is primarily
            provided to allow use of different concurrency models; the default
            is based on :class:`threading.Thread`.
        kwargs (dict): Any additional arguments provided are sent as keyword
            keyword arguments to the underlying
            :class:`~.gapic.pubsub.v1.subscriber_client.SubscriberClient`.
            Generally, you should not need to set additional keyword
            arguments.
    """
    def __init__(self, policy_class=thread.Policy, **kwargs):
        # Sanity check: Is our goal to use the emulator?
        # If so, create a grpc insecure channel with the emulator host
        # as the target.
        if os.environ.get('PUBSUB_EMULATOR_HOST'):
            kwargs['channel'] = grpc.insecure_channel(
                target=os.environ.get('PUBSUB_EMULATOR_HOST'),
            )

        # Use a custom channel.
        # We need this in order to set appropriate default message size and
        # keepalive options.
        if 'channel' not in kwargs:
            kwargs['channel'] = grpc_helpers.create_channel(
                credentials=kwargs.pop('credentials', None),
                target=self.target,
                scopes=subscriber_client.SubscriberClient._DEFAULT_SCOPES,
                options={
                    'grpc.max_send_message_length': -1,
                    'grpc.max_receive_message_length': -1,
                    'grpc.keepalive_time_ms': 30000,
                }.items(),
            )

        # Add the metrics headers, and instantiate the underlying GAPIC
        # client.
        self._api = subscriber_client.SubscriberClient(**kwargs)

        # The subcription class is responsible to retrieving and dispatching
        # messages.
        self._policy_class = policy_class

    @property
    def target(self):
        """Return the target (where the API is).

        Returns:
            str: The location of the API.
        """
        return subscriber_client.SubscriberClient.SERVICE_ADDRESS

    @property
    def api(self):
        """The underlying gapic API client."""
        return self._api

    def subscribe(self, subscription, callback=None, flow_control=()):
        """Return a representation of an individual subscription.

        This method creates and returns a ``Consumer`` object (that is, a
        :class:`~.pubsub_v1.subscriber._consumer.Consumer`)
        subclass) bound to the topic. It does `not` create the subcription
        on the backend (or do any API call at all); it simply returns an
        object capable of doing these things.

        If the ``callback`` argument is provided, then the :meth:`open` method
        is automatically called on the returned object. If ``callback`` is
        not provided, the subscription is returned unopened.

        .. note::
            It only makes sense to provide ``callback`` here if you have
            already created the subscription manually in the API.

        Args:
            subscription (str): The name of the subscription. The
                subscription should have already been created (for example,
                by using :meth:`create_subscription`).
            callback (function): The callback function. This function receives
                the :class:`~.pubsub_v1.types.PubsubMessage` as its only
                argument.
            flow_control (~.pubsub_v1.types.FlowControl): The flow control
                settings. Use this to prevent situations where you are
                inundated with too many messages at once.

        Returns:
            ~.pubsub_v1.subscriber._consumer.Consumer: An instance
                of the defined ``consumer_class`` on the client.

        Raises:
            TypeError: If ``callback`` is not callable.
        """
        flow_control = types.FlowControl(*flow_control)
        subscr = self._policy_class(self, subscription, flow_control)
        if callable(callback):
            subscr.open(callback)
        elif callback is not None:
            error = '{!r} is not callable, please check input'.format(callback)
            raise TypeError(error)
        return subscr

    def subscribe_experimental(
            self, subscription, callback, flow_control=(),
            scheduler_=None):
        """Asynchronously start receiving messages on a given subscription.

        This method starts a background thread to begin pulling messages from
        a Pub/Sub subscription and scheduling them to be processed using the
        provided ``callback``.

        The ``callback`` will be called with an individual
        :class:`google.cloud.pubsub_v1.subscriber.message.Message`. It is the
        responsibility of the callback to either call ``ack()`` or ``nack()``
        on the message when it finished processing. If an exception occurs in
        the callback during processing, the exception is logged and the message
        is ``nack()`` ed.

        The ``flow_control`` argument can be used to control the rate of at
        which messages are pulled. The settings are relatively conservative by
        default to prevent "message hoarding" - a situation where the client
        pulls a large number of messages but can not process them fast enough
        leading it to "starve" other clients of messages. Increasing these
        settings may lead to faster throughput for messages that do not take
        a long time to process.

        This method starts the receiver in the background and returns a
        *Future* representing its execution. Waiting on the future (calling
        ``result()``) will block forever or until a non-recoverable error
        is encountered (such as loss of network connectivity). Cancelling the
        future will signal the process to shutdown gracefully and exit.

        Example

        .. code-block:: python

            from google.cloud.pubsub_v1 import subscriber

            subscriber_client = pubsub.SubscriberClient()

            # existing subscription
            subscription = subscriber_client.subscription_path(
                'my-project-id', 'my-subscription')

            def callback(message):
                print(message)
                message.ack()

            future = subscriber.subscribe_experimental(
                subscription, callback)

            try:
                future.result()
            except KeyboardInterrupt:
                future.cancel()

        Args:
            subscription (str): The name of the subscription. The
                subscription should have already been created (for example,
                by using :meth:`create_subscription`).
            callback (Callable[~.pubsub_v1.subscriber.message.Message]):
                The callback function. This function receives the message as
                its only argument and will be called from a different thread/
                process depending on the scheduling strategy.
            flow_control (~.pubsub_v1.types.FlowControl): The flow control
                settings. Use this to prevent situations where you are
                inundated with too many messages at once.

        Returns:
            google.cloud.pubsub_v1.futures.StreamingPullFuture: A Future object
                that can be used to manage the background stream.
        """
        flow_control = types.FlowControl(*flow_control)

        manager = streaming_pull_manager.StreamingPullManager(
            self, subscription, flow_control)

        future = futures.StreamingPullFuture(manager)

        manager.open(callback)

        return future
